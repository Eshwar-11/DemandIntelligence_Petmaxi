"""
PetMaxi Forecasting Dashboard — Backend v7
=============================================
Reads from petmaxi_v7.db only, never runs models live. Built against the
v7 schema: batch_meta / sku_manifest / sku_forecasts / sku_grid /
sku_daily_forecasts / sku_daily_grid / sku_daily_manifest.

New in this version:
  - /api/freshness         data-recency banner (fade, not urgency color -
                            Track 1 decision: show all forecast values,
                            flag their currency rather than gating them)
  - Smart per-SKU default horizon (Smooth -> Priority_Horizon; non-smooth
    with a high zero rate -> skip 1W, since a near-zero-actual test
    window there is what produces a misleadingly perfect grade)
  - /api/sku/<sku> includes zero_wape_override / untrusted_zero_model /
    data_gap_periods on every horizon row, surfaced rather than hidden
  - 2D/3D exposed as informational-only (no urgency color), gated to
    "insufficient" when grade is Unreliable

sku_inventory does not exist in this db yet (WES inventory is a separate
feed, still to be wired in) - every endpoint that would join it degrades
gracefully to nulls rather than failing, and says so.

Key endpoints:
  GET  /                      -> dashboard HTML
  GET  /api/freshness         -> data-recency banner
  GET  /api/batch_status      -> last run info
  GET  /api/readiness         -> coverage + grade-pill summary
  GET  /api/skus              -> list for portfolio/search views
  GET  /api/attention         -> SKUs by tier, gap-based (inventory-dependent)
  GET  /api/sku/<sku>         -> full detail: all horizons, daily, RM, flags
  POST /api/abc_weights       -> recompute tiers with new weights, no retrain
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

# ── CONFIG ───────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PETMAXI_DB_PATH", os.path.join(_HERE, "db", "petmaxi_v7.db"))
DATA_PATH = os.environ.get("PETMAXI_DATA_PATH", os.path.join(_HERE, "data", "vendas_1_1.xlsx"))
# Bundled NETPET pull used to seed the open-orders store for a no-VPN demo
# (see the startup fetch in __main__). A real live pull always wins over it.
OPEN_ORDERS_FIXTURE = os.environ.get(
    "PETMAXI_ORDERS_FIXTURE", os.path.join(_HERE, "data", "open_orders_fixture.json")
)
TEMPLATE_NAME = "dashboard_v7_ab_api.html"
LANDING_TEMPLATE = "landing_v7_ab_api.html"

DEFAULT_HORIZON_DAYS = 28     # 4W, only used as a last-resort fallback if a
                              # SKU has no valid champion at all - the real
                              # default is the smart per-SKU pick below.
ATTENTION_TOP_N = 5
HORIZON_LABELS = {7: "1W", 14: "2W", 28: "4W", 56: "8W", 91: "13W"}
DAILY_HORIZON_LABELS = {2: "2D", 3: "3D"}
PRIORITY_TOLERANCE_PTS = 3.0     # must match run_batch_v7.py's own constant

# Open orders folded into the production gap:
#   gap = forecast_total + open_order_tons - available
# open_order_tons uses the Pending basis by default (committed demand still on
# the plan, prorated to tonnes in open_orders_store). Switch the basis with one
# env var if the planning definition changes; ordered/reserved/transformed are
# the other choices. See open_orders_store.open_order_gap_tons_all_skus.
OPEN_ORDER_GAP_BASIS = os.environ.get("PETMAXI_OO_GAP_BASIS", "pending")
# gap_pct denominator: "demand" -> gap / (forecast + open_orders) (truer base
# now orders ride in the gap); "forecast" -> legacy gap / forecast.
GAP_PCT_BASE = os.environ.get("PETMAXI_GAP_PCT_BASE", "demand")

# Freshness fade: single-hue (green intensity -> grey), no red/amber - the
# person building this dashboard has mild protanomaly and asked specifically
# for this over the standard traffic-light convention.
FRESH_COLOR = (238, 49, 36)     # Addverb Pomegranate Red as RGB
STALE_COLOR = (153, 153, 140)   # muted warm grey, fades from brand red
FRESHNESS_FULLY_STALE_WEEKS = 8.0   # 2 missed fortnightly cycles = full fade

app = Flask(__name__)   # dashboard_v7.html lives in templates/, Flask's
                         # default - no override needed. (Earlier this was
                         # forced to _HERE because the file was flat next
                         # to the script; now that it's back in templates/,
                         # forcing it would cause TemplateNotFound again.)
CORS(app)


# ── DB helpers ────────────────────────────────────────────────────────────────

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
    """{sku: open-order tonnes} for the gap, empty on any store error so a
    missing/unreachable orders DB degrades to 'no open orders', never a 500."""
    try:
        return oo_store.open_order_gap_tons_all_skus(basis or OPEN_ORDER_GAP_BASIS)
    except Exception as e:
        print(f"WARN: open-orders gap map failed: {e}")
        return {}


def _resolve_inventory(conn):
    """(available_map, source) for the FG gap.

    Prefers the live NETPET inventory snapshot: it is positive-stock-only, so a
    SKU absent from it genuinely has 0 tonnes available and the gap is
    computable for every SKU (source 'netpet_api'). Falls back to the legacy
    sku_inventory table ('sku_inventory_table', absence = uncovered), else no
    coverage (None)."""
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
    """(available_tons, source) for one SKU. Same precedence as
    _resolve_inventory; opens its own short-lived connection for the table
    fallback so it can be called after the request connection is closed."""
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


# ── Unit conversion factors (QTsacs, pallets) ───────────────────────────────
_conv_cache = {}

def _load_sack_weights():
    """WEIGHT column from the SKU_FAMILY sheet = kg per sack, the same fixed
    per-SKU factor data_prep_v1 uses for QTTON = QTSAC * WEIGHT / 1000."""
    global _conv_cache
    if "sack" in _conv_cache:
        return _conv_cache["sack"]
    try:
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
    """Per-SKU conversion factors. kg_per_pallet is DERIVED from the WES
    pallet snapshot (avg pallet weight), not declared packaging master data -
    labelled as such so nobody mistakes it for a confirmed spec. Missing
    factors come back None and render as an em-dash-free blank downstream."""
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


# ── RM helper (BOM explosion from FG forecast, reads Excel formula sheets) ───
_rm_cache = {}

def _load_rm_data():
    global _rm_cache
    if _rm_cache and not _rm_cache.get("sku_formula", pd.DataFrame()).empty:
        return _rm_cache
    try:
        if not os.path.exists(DATA_PATH):
            print(f"WARN: RM data source not found at {DATA_PATH} - RM requirements unavailable")
            return {"sku_formula": pd.DataFrame(), "formula": pd.DataFrame()}
        sf = pd.read_excel(DATA_PATH, sheet_name="sku_formula")
        fm = pd.read_excel(DATA_PATH, sheet_name="Formula")
        # sku_formula's SKU column is mixed-type object dtype - purely numeric
        # codes (5900855) load as Python int, alphanumeric ones (HAPPYESTERIL10)
        # as str, so comparing against the URL's string sku_id silently failed
        # to match every numeric-looking SKU. Force to str once, here.
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


# ── ABC velocity tiers (recomputed on request, never retrained) ─────────────

def _compute_tiers(manifest_rows, weight_volume=0.7, weight_frequency=0.3):
    df = pd.DataFrame(manifest_rows)
    if df.empty:
        return df
    # Drop any incoming tier before recomputing, or the merge below collides
    # into velocity_tier_x/_y instead of cleanly overwriting it.
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


# ── Smart per-SKU default horizon ────────────────────────────────────────────
# Smooth SKUs: Priority_Horizon (shortest horizon within tolerance of the
# true best WAPE for that SKU). Non-smooth (Intermittent/Erratic/Lumpy) with
# a high zero rate: skip 1W specifically, since a near-zero-actual test
# window there is exactly what produces a misleadingly perfect grade -
# Shivani's ABC-XYZ/SPEC-metric work is the intended long-term replacement
# for this heuristic, so this stays isolated in one function.

HIGH_ZERO_RATE_THRESHOLD = 0.3   # matches the Erratic/Lumpy boundary already
                                  # used in forecasting_engine_v7's demand
                                  # pattern classification, one definition
                                  # used everywhere rather than two.

def _priority_horizon(valid_forecasts: list) -> Optional[dict]:
    """valid_forecasts: rows with non-null wape, any order. Returns the
    shortest horizon within PRIORITY_TOLERANCE_PTS of the true best."""
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
    """Pure lowest-WAPE horizon, no tolerance shortening. This is the
    honest 'most accurate' answer, shown as a separate reference fact
    alongside Priority_Horizon, never blended into it."""
    if not valid_forecasts:
        return None
    return min(valid_forecasts, key=lambda r: r["wape"])


def _primary_horizon(forecasts_by_horizon: dict) -> Optional[dict]:
    """The horizon shown everywhere by default: 1W, fixed, for every SKU,
    no per-SKU smoothness exception - Best_Horizon and Priority_Horizon
    are exposed separately as options the user can manually pivot to,
    they never silently substitute for 1W here. Only falls back (to
    Priority_Horizon) when a SKU genuinely has no 1W row at all, since
    showing nothing would be less honest than showing the best available
    alternative with that alternative clearly labelled."""
    valid = [f for f in forecasts_by_horizon.values() if f.get("wape") is not None]
    if not valid:
        return None
    one_week = forecasts_by_horizon.get(7) or forecasts_by_horizon.get(1)
    if one_week and one_week.get("wape") is not None:
        return one_week
    return _priority_horizon(valid)


# ── Freshness color interpolation ────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

BASE_PATH = os.environ.get("PETMAXI_BASE_PATH", "/petmaxi-dashboard")

@app.route("/")
def landing():
    return render_template(LANDING_TEMPLATE, base_path=BASE_PATH)

@app.route("/forecast")
def forecast():
    return render_template(TEMPLATE_NAME, base_path=BASE_PATH)

@app.route("/analytics")
def analytics():
    return render_template("Metric_Analytics_ab.html")

@app.route("/customers")
def customers():
    return render_template("Customer_Intelligence_ab.html")


@app.route("/api/freshness")
def api_freshness():
    """Data-recency banner. Fades on a single hue (green -> muted grey-
    green), by design (see FRESH_COLOR/STALE_COLOR comment above) - this is
    a currency flag on values that ARE shown, not a gate on showing them
    (Track 1 decision)."""
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
        "ready": True,
        "gap_weeks": gap_weeks,
        "gap_days": gap_days,
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
        "ready": True,
        "run_at": meta["run_at"],
        "total_skus": meta["total_skus"],
        "forecastable": meta["forecastable"],
        "status": meta["status"],
        "duration_sec": meta["duration_sec"],
        "gap_weeks": _safe_float(meta, "gap_weeks"),
        "gap_days": _safe_float(meta, "gap_days"),
    })


@app.route("/api/readiness")
def api_readiness():
    """Coverage banner + grade pills. Grade pills reflect each SKU's own
    smart default horizon, not one fixed horizon for the whole portfolio,
    since the default horizon now genuinely varies SKU to SKU."""
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
        "ready": True,
        "total_skus": total_all,
        "forecastable": fore_all,
        "pct_ready": round(fore_all / total_all * 100, 1) if total_all else 0,
        "last_run": last_run["run_at"] if last_run else None,
        "by_tier": tiers,
        "by_grade": grade_counts,
    })


@app.route("/api/skus")
def api_skus():
    """SKU list for portfolio/search views, each with its smart default
    horizon already resolved so the frontend doesn't need to."""
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


ATTENTION_HORIZON_DAYS = 7   # Banner cards are always computed at 1W (fixed,
                             # every SKU); the table re-fetches at whatever
                             # planning window the user selects. Same endpoint
                             # serves both, distinguished by the horizon param.

@app.route("/api/attention")
def api_attention():
    """Legacy-shape endpoint matching the deployed frontend exactly:
    ?horizon=<weeks> (1,2,4,8,13), returns {tiers: {A,B,C: {label,
    attention[], sufficient[], attention_total, sufficient_total}}} with
    per-row urgency red/yellow/green. v7 additions ride along as extra
    fields on each row (best_horizon_*, zero_wape_override,
    data_gap_periods) without changing the legacy shape."""
    if not _db_exists():
        return jsonify({"ready": False, "message": "No batch run yet."})

    horizon_weeks = int(request.args.get("horizon", 1))
    horizon_days = {1: 7, 2: 14, 4: 28, 8: 56, 13: 91}.get(horizon_weeks, horizon_weeks * 7)
    top_n = int(request.args.get("top_n", 999))
    tier_filter = request.args.get("tier", "").upper()

    conn = _db()
    has_inventory = _table_exists(conn, "sku_inventory")
    manifest = pd.read_sql("SELECT * FROM sku_manifest WHERE is_forecastable = 1", conn)
    forecasts_h = pd.read_sql(
        "SELECT * FROM sku_forecasts WHERE horizon_days = ?", conn, params=(horizon_days,)
    )
    all_forecasts = pd.read_sql("SELECT sku, horizon_days, wape, best_model FROM sku_forecasts", conn)
    inventory = pd.read_sql("SELECT * FROM sku_inventory", conn) if has_inventory else pd.DataFrame()
    # Resolve the FG available map (live NETPET snapshot preferred) and the
    # open-order tonnes map before the connection closes.
    inv_map, inv_source = _resolve_inventory(conn)
    conn.close()
    oo_map = _safe_oo_map()

    if tier_filter in ("A", "B", "C"):
        manifest = manifest[manifest["velocity_tier"] == tier_filter]

    merged = manifest.merge(forecasts_h, on="sku", how="left", suffixes=("", "_f"))
    if not inventory.empty:
        merged = merged.merge(inventory, on="sku", how="left", suffixes=("", "_i"))

    by_tier = {"A": {"attention": [], "sufficient": []},
               "B": {"attention": [], "sufficient": []},
               "C": {"attention": [], "sufficient": []}}

    for _, r in merged.iterrows():
        sku_str = str(r["sku"])
        forecast_total = r.get("forecast_total")
        source_tag = r.get("source_tag") if has_inventory else None
        forecast_total = None if pd.isna(forecast_total) else forecast_total
        source_tag = None if pd.isna(source_tag) else source_tag

        # Available AND On Hand come from the resolved inventory source (live
        # NETPET preferred), never the legacy sku_inventory on_hand column -
        # that table is a stale May WES extract and its on_hand was showing on
        # the SKU card while Available was live, so the two disagreed. NETPET
        # carries no allocation yet (reserved->allocated pending client
        # sign-off), so On Hand == Available here; when allocation is wired,
        # Available = On Hand - Allocated still holds. NETPET is
        # positive-stock-only, so a SKU missing there is genuinely 0 tonnes;
        # the legacy table leaves a missing SKU uncovered.
        if inv_source == "netpet_api":
            available = inv_map.get(sku_str, 0.0)
            source_tag = source_tag or "NETPET"
        elif inv_source == "sku_inventory_table":
            available = inv_map.get(sku_str)
        else:
            available = None
        # On Hand mirrors the resolved available (no legacy on_hand reference).
        on_hand = available

        # Open orders folded into the gap (default Pending basis, tonnes).
        oo_tons = round(oo_map.get(sku_str, 0.0), 3)
        demand_total = (forecast_total + oo_tons) if forecast_total is not None else None

        if forecast_total is not None and available is not None:
            gap = round(forecast_total + oo_tons - available, 3)
            denom = demand_total if GAP_PCT_BASE == "demand" else forecast_total
            gap_pct = round((gap / denom * 100) if denom and denom > 0 else 0, 1)
            threshold = 0 if horizon_weeks == 1 else 20
            urgency = "red" if gap_pct > 50 else ("yellow" if gap_pct > threshold else "green")
            bucket = "attention" if gap_pct > threshold else "sufficient"
        else:
            # No inventory coverage at all (no NETPET snapshot, no table): cannot
            # compute a gap honestly. Sits in sufficient/green so it never
            # falsely lands in Urgent; open orders still ride along as a field.
            gap, gap_pct, urgency, bucket = 0, 0, "green", "sufficient"

        sku_all = all_forecasts[(all_forecasts["sku"] == r["sku"]) & all_forecasts["wape"].notna()]
        best_row = sku_all.loc[sku_all["wape"].idxmin()] if not sku_all.empty else None

        card = {
            "sku": r["sku"], "description": r.get("description"), "family": r.get("family"),
            "velocity_tier": r.get("velocity_tier"),
            "best_model": r.get("best_model"),
            "wape": round(r["wape"], 2) if pd.notna(r.get("wape")) else None,
            "grade": r.get("grade") or "No Model",
            "forecast_total": round(forecast_total, 2) if forecast_total is not None else 0,
            "open_order_tons": oo_tons,
            "open_order_basis": OPEN_ORDER_GAP_BASIS,
            "demand_total": round(demand_total, 2) if demand_total is not None else 0,
            "on_hand": round(on_hand, 2) if on_hand is not None else 0,
            "available": round(available, 2) if available is not None else 0,
            "source_tag": source_tag,   # was missing entirely - the frontend's
                                          # WES badge/filter reads this per row
            "gap": gap, "gap_pct": gap_pct, "urgency": urgency,
            "horizon_weeks": horizon_weeks,
            "inventory_covered": available is not None,
            "best_horizon_days": int(best_row["horizon_days"]) if best_row is not None else None,
            "best_horizon_label": HORIZON_LABELS.get(int(best_row["horizon_days"])) if best_row is not None else None,
            "best_wape": round(best_row["wape"], 2) if best_row is not None else None,
            "zero_wape_override": bool(r.get("zero_wape_override")),
            "data_gap_periods": int(r["data_gap_periods"]) if pd.notna(r.get("data_gap_periods")) else None,
        }
        tier = card["velocity_tier"] if card["velocity_tier"] in by_tier else "C"
        by_tier[tier][bucket].append(card)

    tier_labels = {"A": "Fast Movers", "B": "Medium Movers", "C": "Slow Movers"}
    result = {}
    for t in ("A", "B", "C"):
        attn = sorted(by_tier[t]["attention"], key=lambda x: -(x["gap"] or 0))
        suff = sorted(by_tier[t]["sufficient"], key=lambda x: (x["gap"] or 0))
        result[t] = {"label": tier_labels[t], "attention": attn[:top_n], "attention_total": len(attn),
                     "sufficient": suff[:top_n], "sufficient_total": len(suff)}

    return jsonify({"ready": True, "horizon_weeks": horizon_weeks,
                    "inventory_available": inv_source is not None,
                    "inventory_source": inv_source,
                    "open_order_basis": OPEN_ORDER_GAP_BASIS,
                    "tiers": result})


@app.route("/api/sku/<sku_id>")
def api_sku_detail(sku_id: str):
    """Full detail for one SKU: every weekly horizon (with zero-WAPE /
    gap flags surfaced, never silently hidden), gated daily 2D/3D, RM
    requirements, and the resolved smart default horizon."""
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
        h = f["horizon_weeks"]   # keyed by WEEKS (1,2,4,8,13) - the deployed
                                  # frontend indexes d.forecasts[1], d.forecasts[4]
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
            "grade": grade,
            "gated": gated,
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

    # Available: live NETPET FG snapshot preferred, legacy table as fallback.
    available_val, av_source = _available_for_sku(sku_id)
    oo_tons = 0.0
    try:
        oo_tons = round(oo_store.open_order_gap_tons_for_sku(sku_id, OPEN_ORDER_GAP_BASIS), 3)
    except Exception as e:
        print(f"WARN: open-orders gap for {sku_id} failed: {e}")

    # Inventory block for the SKU card. On Hand and Available BOTH come from the
    # resolved live source (available_val); the legacy sku_inventory on_hand /
    # allocated columns are no longer read here - that stale May WES extract was
    # showing as On Hand while Available was live, so the card disagreed with
    # itself. NETPET carries no allocation yet (reserved->allocated pending
    # client sign-off) so Allocated is 0 and On Hand == Available today; once
    # allocation is wired, Available = On Hand - Allocated still holds.
    # open_order_tons/basis are new fields the Order Breakdown card surfaces.
    inv_val = round(available_val, 2) if available_val is not None else 0
    inventory = {
        "on_hand": inv_val,
        "allocated": 0,
        "available": inv_val,
        "source_tag": "NETPET" if av_source == "netpet_api" else None,
        "covered": available_val is not None,
        "open_order_tons": oo_tons,
        "open_order_basis": OPEN_ORDER_GAP_BASIS,
    }

    # gap = forecast + open_order_tons - available. available defaults to 0
    # only when there is no inventory source at all (same as the prior
    # behaviour), and inventory.covered flags that case honestly.
    avail_for_gap = available_val if available_val is not None else inventory["available"]
    gaps = {}
    for h, fc in forecasts.items():
        if fc["forecast_total"] is not None:
            gaps[h] = round(fc["forecast_total"] + oo_tons - avail_for_gap, 2)

    # RM defaults off the primary (1W-first) view, consistent with what's
    # shown everywhere else by default - switching the dashboard's horizon
    # capsule should be what changes this, not a smarter hidden pick.
    fg_total_for_rm = primary["forecast_total"] if primary and primary.get("forecast_total") else 0.0
    rm = _get_rm_forecast(sku_id, fg_total_for_rm)

    def _horizon_ref(h):
        if not h:
            return None
        return {"horizon_days": h["horizon_days"], "horizon_label": HORIZON_LABELS.get(h["horizon_days"]),
                "wape": round(h["wape"], 2), "model": h.get("best_model")}

    return jsonify({
        "sku": sku_id,
        "description": manifest["description"],
        "family": manifest["family"],
        "velocity_tier": manifest["velocity_tier"],
        "velocity": {"tier": manifest["velocity_tier"]},
        "forecastability": {
            "is_forecastable": bool(manifest["is_forecastable"]),
            "status": manifest["status"],
            "demand_pattern": manifest["demand_pattern"],
            "adi": _safe_float(manifest, "adi"),
            "cv2": _safe_float(manifest, "cv2"),
            "zero_pct": _safe_float(manifest, "zero_pct"),
        },
        "default_horizon_days": primary["horizon_days"] if primary else None,
        "default_horizon_label": HORIZON_LABELS.get(primary["horizon_days"]) if primary else None,
        "conversions": _get_conversions(sku_id, inv),
        "best_horizon": _horizon_ref(best),
        "priority_horizon": _horizon_ref(priority),
        "inventory": inventory,
        "forecasts": forecasts,
        "daily_forecasts": daily,
        "gaps": gaps,
        "rm_requirements": rm,
    })


@app.route("/api/external_inventory")
def api_external_inventory():
    """WES inventory SKUs that have no forecast (not in sku_manifest) -
    powers the deployed dashboard's 'WES New Entries' panel."""
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


# ─────────────────────────────────────────────────────────────────────────────
# OPEN ORDERS (NETPET pending-orders feed, separate petmaxi_open_orders.db)
# ─────────────────────────────────────────────────────────────────────────────
# These three routes read/refresh a snapshot store that lives in its OWN sqlite
# file (open_orders_store.ORDERS_DB_PATH), untouched by the 14-day forecast batch
# upload. Order quantities are reported as four OVERLAPPING views - Ordered is
# the total commitment, Reserved is the part held against FG stock, Pending is
# the part on the production plan, Transformed is the produced part of Pending -
# and must never be summed. The frontend shows them side by side, not added, and
# does not fold them into the forecast gap.

@app.route("/api/open_orders")
def api_open_orders():
    """Per-SKU open-order aggregate for the SKU list, plus the fetch stamps.
    by_sku is keyed by SKU string so the frontend looks up each row directly.
    last_success drives the 'fetched X ago' header (never a failed attempt);
    last_fetch is the most recent attempt of any status, for diagnostics."""
    try:
        by_sku = oo_store.aggregate_all_skus()
    except Exception as e:
        return jsonify({"ready": False, "by_sku": {}, "last_success": None,
                        "last_fetch": None, "error": str(e)})
    return jsonify({
        "ready": True,
        "by_sku": by_sku,
        "last_success": oo_store.get_last_success(),
        "last_fetch": oo_store.get_last_fetch(),
    })


@app.route("/api/sku/<sku_id>/open_orders")
def api_sku_open_orders(sku_id: str):
    """Everything the detail modal's Order Breakdown card needs for one SKU:
    the rolled-up aggregate (with its interpretation_note) and one row per
    open-order line for this SKU, joined back to its parent order fields."""
    try:
        aggregate = oo_store.aggregate_for_sku(sku_id)
        orders = oo_store.get_items_for_sku(sku_id)
    except Exception as e:
        return jsonify({"ready": False, "sku": sku_id, "aggregate": None,
                        "orders": [], "error": str(e)})
    return jsonify({
        "ready": True,
        "sku": sku_id,
        "aggregate": aggregate,
        "orders": orders,
        "last_success": oo_store.get_last_success(),
    })


@app.route("/api/open_orders/refresh", methods=["POST"])
def api_open_orders_refresh():
    """Manual refresh. Body {"source":"live"} (default) hits NETPET and needs
    VPN + NETPET_BASE_URL; {"source":"fixture"[,"path":...]} loads a local JSON.
    On any failure the previous snapshot is preserved intact (the store never
    partially writes) - we return HTTP 200 with ok:false so the frontend can
    show its 'connect to VPN' tip while continuing to serve the last good data,
    rather than treating it as a hard error."""
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
        return jsonify({"ok": False, "source": source, "error": str(e),
                        "last_success": oo_store.get_last_success(),
                        "last_fetch": oo_store.get_last_fetch()}), 200


# ─────────────────────────────────────────────────────────────────────────────
# NETPET INVENTORY (live FG/RM stock, separate petmaxi_inventory.db)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/inventory_status")
def api_inventory_status():
    """Whether a live NETPET inventory snapshot is loaded, and its stamps.
    The gap uses this snapshot as `available` when present."""
    try:
        return jsonify({
            "ready": True,
            "has_inventory": inv_store.has_inventory(),
            "last_success": inv_store.get_last_success(),
            "last_fetch": inv_store.get_last_fetch(),
        })
    except Exception as e:
        return jsonify({"ready": False, "has_inventory": False, "error": str(e)})


@app.route("/api/inventory/refresh", methods=["POST"])
def api_inventory_refresh():
    """Manual NETPET inventory refresh. Body {"source":"live"} (default) hits
    NETPET /inventory/ and needs VPN + NETPET_BASE_URL; {"source":"fixture",
    "path":...} loads a local JSON. On failure the previous snapshot is kept
    (never a partial write); returns HTTP 200 ok:false so the frontend keeps
    serving the last good data."""
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
        return jsonify({"ok": False, "source": source, "error": str(e),
                        "last_success": inv_store.get_last_success(),
                        "last_fetch": inv_store.get_last_fetch()}), 200


HORIZON_SUMMARY_DAYS = [7, 14, 28, 56, 91]   # 1W / 2W / 4W / 8W / 13W for the landing card

@app.route("/api/horizon_summary")
def api_horizon_summary():
    """Per-horizon roll-up powering the landing graphic's cycling numbers and
    its tonnage line. For each of 1W/2W/4W/8W: total forecast tonnage, reliability
    (% of modelled SKUs graded Excellent/Good), and SKUs modelled at that horizon.
    'Modelled' = a forecast row with a real WAPE (a model was actually evaluated)."""
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
            "horizon_days": hd,
            "horizon_label": HORIZON_LABELS.get(hd, f"{hd}d"),
            "total_forecast_tons": round(total_t, 1),
            "reliability_pct": round(good / n * 100, 1) if n else 0.0,
            "n_skus_modelled": n,
        })
    return jsonify({"ready": True, "horizons": out})


def _persist_tiers(conn, recomputed, w_vol, w_freq):
    """Writes recomputed velocity tiers back to sku_manifest and stores the
    chosen weights in abc_config (single-row) so the change survives and the
    next batch can honour it. No model run - this is a pure re-ranking."""
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
    """Recompute velocity tiers with new weights (no retrain). With
    persist=true (the dashboard's Apply), the recomputed tiers are written back
    to sku_manifest so every downstream view - readiness, attention, the SKU
    list behind each action card - reflects them immediately, and the weights
    are stored in abc_config for the next batch. persist=false previews only."""
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
        "success": True,
        "persisted": persist,
        "weight_volume": w_vol, "weight_frequency": w_freq,
        "tier_counts": {"A": counts.get("A", 0), "B": counts.get("B", 0), "C": counts.get("C", 0)},
        "n_moved": moved,
        "note": ("Applied and persisted - tiers updated in the database and weights "
                 "saved for the next batch run."
                 if persist else
                 "Preview only - not persisted."),
    })


if __name__ == "__main__":
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
    from werkzeug.serving import run_simple

    def _startup_open_orders_fetch():
        """One-shot open-orders pull at boot. A VPN-down / NETPET_BASE_URL-unset
        failure must NOT stop the server: we log it (surfaced to the frontend via
        last_fetch) and keep serving whatever snapshot exists. If there is NO
        snapshot at all, seed the bundled fixture so a no-VPN demo isn't empty -
        but never overwrite a snapshot that already loaded, so a real pull wins."""
        oo_store.init_db()
        try:
            res = oo_store.fetch_and_store()
            print(f"[open_orders] startup live fetch OK: {res}")
            return
        except Exception as e:
            print(f"[open_orders] startup live fetch FAILED (serving previous snapshot): {e}")
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
        """One-shot NETPET inventory pull at boot, same resilience contract as
        open orders: a VPN-down / base-URL-unset failure is logged and the server
        keeps serving the previous snapshot (or none). No fixture seed - inventory
        has no bundled demo file; the gap falls back to the legacy sku_inventory
        table (or 'uncovered') when there is no snapshot."""
        inv_store.init_db()
        try:
            res = inv_store.fetch_and_store()
            print(f"[inventory] startup live fetch OK: {res}")
        except Exception as e:
            print(f"[inventory] startup live fetch FAILED (serving previous snapshot): {e}")
            try:
                inv_store.log_failure("live-startup", str(e))
            except Exception:
                pass

    USE_RELOADER = True
    # run_simple's reloader runs this file twice (a supervisor parent + the
    # serving child that has WERKZEUG_RUN_MAIN=true). Fire the fetches only in
    # the serving process so a boot pull doesn't run twice; if the reloader is
    # ever disabled, WERKZEUG_RUN_MAIN is unset and we fall back to firing here.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not USE_RELOADER:
        _startup_open_orders_fetch()
        _startup_inventory_fetch()

    print(f"DB path:   {DB_PATH}  (exists: {os.path.exists(DB_PATH)})")
    print(f"Data path: {DATA_PATH}  (exists: {os.path.exists(DATA_PATH)})")
    print(f"Serving at http://localhost:5000{BASE_PATH}/  (or http://<this-machine's-LAN-IP>:5000{BASE_PATH}/ from another machine)")
    print(f"NOTE: binds to 0.0.0.0 (all interfaces) - do not put 0.0.0.0 itself in a browser, it isn't a reachable address.")

    # Mounts the whole app under BASE_PATH ("/petmaxi-dashboard" by default,
    # override with PETMAXI_BASE_PATH) - this is what actually makes the
    # dashboard live at that URL. Setting PETMAXI_BASE_PATH alone (used
    # elsewhere for asset URLs inside the page) does NOT do this by itself.
    application = DispatcherMiddleware(Flask("dummy_root"), {BASE_PATH or "/petmaxi-dashboard": app})
    run_simple("0.0.0.0", 5000, application, use_reloader=USE_RELOADER, use_debugger=True)
