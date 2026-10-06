"""
run_batch_v7.py
================
PetMaxi Batch Forecast Job — v7 (daily 2D/3D merged in)

Pipeline: raw xlsx -> data_prep_v1 (zero-fill, export exclusion, outlier
cap) -> forecasting_engine_v7 (expanded model registry, genuine
per-horizon grid search, Insufficient History gate, zero-WAPE trust
gate, gap-bridging production dates) -> outputs:

  1. Timestamped SQLite db with:
       batch_meta, sku_manifest, sku_forecasts, sku_grid   (weekly, as before)
       sku_daily_forecasts, sku_daily_grid, sku_daily_manifest  (NEW: 2D/3D,
       merged into this same run instead of a separate standalone script)
  2. Long-format tracker CSV (weekly): one row per SKU per horizon.
  3. Manager's SKU x Horizon matrix CSV (weekly): Priority_Horizon /
     Priority_WAPE / Best_Horizon / Best_WAPE, per SKU.
  4. Daily forecast CSV (2D/3D): one row per SKU per daily horizon.

Forecast dates (both weekly and daily) are anchored to the real
batch-run date, not to the last date actually present in the data. If
the underlying vendas data is stale, the champion model for each
horizon is asked for the extra (gap) periods needed to bridge from its
last real data point to today, in one direct multi-step call, and only
the trailing horizon periods are kept and dated from today. The bridged
gap size is written to batch_meta and to every forecast row
(data_gap_periods) so it can be surfaced to the dashboard rather than
silently hidden - see forecasting_engine_v7.py's module docstring, fix
#6, for the full rationale.

Usage:
    python run_batch_v7.py --input vendas_1.xlsx --db-out petmaxi_v7.db
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

import data_prep_v1 as prep
import forecasting_engine_v8 as engine

# ── CONFIG ───────────────────────────────────────────────────────────────────

ABC_WEIGHT_VOLUME    = 0.7
ABC_WEIGHT_FREQUENCY = 0.3
ABC_TIER_A_PCT       = 0.20
ABC_TIER_B_PCT       = 0.30

HORIZON_LABELS = {7: "1W", 14: "2W", 28: "4W", 56: "8W", 91: "13W"}
DAILY_HORIZON_LABELS = {2: "2D", 3: "3D"}
PRIORITY_TOLERANCE_PTS = 3.0   # matches "2-3 pts" agreed, fixed at 3


# ── ABC VELOCITY TIERS ───────────────────────────────────────────────────────
# Unchanged structure: weighted blend of total volume and active-week
# frequency, weights user-adjustable (see dashboard's ABC weights
# modal), fixed 20/30/50 split. ADI/CV2 are NOT part of this - they
# stay internal to demand-pattern classification and the zero-WAPE
# smoothness gate, never mixed into velocity.

def compute_abc_tiers(manifest_df: pd.DataFrame,
                       weight_volume: float = ABC_WEIGHT_VOLUME,
                       weight_frequency: float = ABC_WEIGHT_FREQUENCY) -> pd.DataFrame:
    df = manifest_df.copy()
    vol_rank  = df["total_volume"].rank(pct=True)
    freq_rank = df["n_active"].rank(pct=True)
    df["velocity_score"] = weight_volume * vol_rank + weight_frequency * freq_rank

    ranked = df.sort_values("velocity_score", ascending=False).reset_index(drop=True)
    n = len(ranked)
    n_a = max(1, int(n * ABC_TIER_A_PCT))
    n_b = max(1, int(n * ABC_TIER_B_PCT))

    tiers = (["A"] * n_a) + (["B"] * n_b) + (["C"] * (n - n_a - n_b))
    ranked["velocity_tier"] = tiers[:n]

    return df.merge(ranked[["sku", "velocity_tier"]], on="sku", how="left")


# ── MANAGER'S SKU x HORIZON MATRIX (weekly only, unchanged) ─────────────────

def build_manager_matrix(all_forecast_rows: list) -> pd.DataFrame:
    df = pd.DataFrame(all_forecast_rows)
    if df.empty:
        return pd.DataFrame()
    df["sku"] = df["sku"].astype(str)

    wape_pivot = df.pivot_table(index="sku", columns="horizon_days", values="wape", aggfunc="first")
    model_pivot = df.pivot_table(index="sku", columns="horizon_days", values="best_model", aggfunc="first")
    wape_pivot = wape_pivot.rename(columns=HORIZON_LABELS)
    model_pivot = model_pivot.rename(columns=HORIZON_LABELS)
    horizon_cols = [HORIZON_LABELS[h] for h in sorted(HORIZON_LABELS) if HORIZON_LABELS[h] in wape_pivot.columns]
    wape_pivot = wape_pivot[horizon_cols]
    model_pivot = model_pivot[[c for c in horizon_cols if c in model_pivot.columns]]

    rows = []
    for sku in wape_pivot.index:
        wape_row = wape_pivot.loc[sku]
        model_row = model_pivot.loc[sku] if sku in model_pivot.index else pd.Series(dtype=object)
        valid = wape_row.dropna()
        if valid.empty:
            rows.append({"sku": sku, "Priority_Horizon": None, "Priority_WAPE": None, "Priority_Model": None,
                         "Best_Horizon": None, "Best_WAPE": None, "Best_Model": None})
            continue

        best_horizon = valid.idxmin()
        best_wape = valid.min()
        best_model = model_row.get(best_horizon)

        ordered = [h for h in horizon_cols if h in valid.index]
        best_pos = ordered.index(best_horizon)
        priority_horizon, priority_wape = best_horizon, best_wape
        for h_label in ordered[:best_pos]:
            if valid[h_label] <= best_wape + PRIORITY_TOLERANCE_PTS:
                priority_horizon, priority_wape = h_label, valid[h_label]
                break
        priority_model = model_row.get(priority_horizon)

        rows.append({"sku": sku, "Priority_Horizon": priority_horizon,
                     "Priority_WAPE": round(priority_wape, 2), "Priority_Model": priority_model,
                     "Best_Horizon": best_horizon, "Best_WAPE": round(best_wape, 2), "Best_Model": best_model})

    summary = pd.DataFrame(rows).set_index("sku")

    combined = pd.DataFrame(index=wape_pivot.index)
    for h in horizon_cols:
        combined[f"{h}_WAPE"] = wape_pivot[h]
        combined[f"{h}_Model"] = model_pivot[h] if h in model_pivot.columns else None

    result = combined.join(summary).reset_index()
    result["sku"] = result["sku"].astype(str)
    return result


# ── DAILY DATA (2D/3D), built from the SAME scoped frame the weekly prep
#    already loaded - no second raw-file read ─────────────────────────────

def build_daily_series(scoped: pd.DataFrame) -> tuple:
    """
    scoped is data_prep_v1.filter_scope's output (already loaded once by
    run_batch for the weekly pipeline) - has year/month/day and QTTON
    per transaction row. Aggregates to (SKU, date) daily totals,
    zero-fills each SKU across its own active range up to the
    portfolio-wide max date (same convention as the weekly zero-fill),
    returns {sku: np.ndarray} plus the portfolio's real last daily date.
    """
    daily_scope = scoped.copy()
    daily_scope["date"] = pd.to_datetime(
        dict(year=daily_scope["year"], month=daily_scope["month"], day=daily_scope["day"]), errors="coerce")
    daily_scope = daily_scope.dropna(subset=["date"])

    daily = daily_scope.groupby(["SKU", "date"])["QTTON"].sum().reset_index().rename(columns={"QTTON": "qtton"})
    dataset_max_date = daily["date"].max()

    series_by_sku = {}
    for sku, grp in daily.groupby("SKU"):
        grp = grp.sort_values("date")
        full_range = pd.date_range(start=grp["date"].min(), end=dataset_max_date, freq="D")
        reindexed = grp.set_index("date").reindex(full_range).fillna(0.0)
        series_by_sku[sku] = reindexed["qtton"].values.astype(float)

    return series_by_sku, dataset_max_date


# ── PARALLEL WORKER (each SKU is fully independent, safe to distribute) ─────

def _process_one_sku_worker(task):
    """
    Runs engine.process_sku for one SKU (weekly + daily together) inside
    a worker process. Module level (not a closure/lambda) so it can be
    pickled for Windows' spawn-based multiprocessing.
    """
    (sku, series_list, start_date_str, gap_weeks,
     daily_series_list, daily_start_date_str, gap_days) = task

    series = np.array(series_list, dtype=float)
    start_date = pd.Timestamp(start_date_str)

    daily_series = np.array(daily_series_list, dtype=float) if daily_series_list is not None else None
    daily_start_date = pd.Timestamp(daily_start_date_str) if daily_start_date_str is not None else None

    result = engine.process_sku(sku, series, start_date=start_date, gap_weeks=gap_weeks,
                                 daily_series=daily_series, daily_start_date=daily_start_date,
                                 gap_days=gap_days)
    return sku, result


# ── MAIN BATCH ────────────────────────────────────────────────────────────────

def get_already_processed_skus(db_out: str) -> set:
    """Auto-resume support: check which SKUs already have a manifest row
    in the target db. A SKU only lands in sku_manifest once BOTH its
    weekly and daily processing completed in the same worker call, so
    this one check is sufficient for both. Returns empty set if the db
    or table doesn't exist yet (first run)."""
    if not os.path.exists(db_out):
        return set()
    try:
        conn = sqlite3.connect(db_out)
        existing = pd.read_sql("SELECT sku FROM sku_manifest", conn)
        conn.close()
        return set(existing["sku"])
    except Exception:
        return set()


def _safe_to_csv(df: pd.DataFrame, path: str, label: str) -> bool:
    """
    Writes a CSV, but never lets a locked file (e.g. open in Excel,
    causing a permission-denied error) crash the whole run after the
    expensive model computation already succeeded.
    """
    try:
        df.to_csv(path, index=False)
        return True
    except PermissionError:
        print(f"    WARN: could not write {label} -> {path} (file is open elsewhere, likely in Excel).")
        print(f"         Close the file and rerun with --export-only to regenerate it without reprocessing.")
        return False
    except Exception as e:
        print(f"    WARN: could not write {label} -> {path}: {e}")
        return False


def rebuild_csvs_from_db(db_out: str, tracker_csv_out: str, matrix_csv_out: str,
                          grid_csv_out: str, daily_csv_out: str) -> None:
    """
    Rebuilds all CSVs from whatever is currently accumulated in the db,
    across all installments so far, not just the batch just processed.
    """
    conn = sqlite3.connect(db_out)
    manifest_df = pd.read_sql("SELECT * FROM sku_manifest", conn)
    forecasts_df = pd.read_sql("SELECT * FROM sku_forecasts", conn)
    grid_df = pd.read_sql("SELECT * FROM sku_grid", conn) if pd.read_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sku_grid'", conn).shape[0] else pd.DataFrame()
    daily_df = pd.read_sql("SELECT * FROM sku_daily_forecasts", conn) if pd.read_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sku_daily_forecasts'", conn).shape[0] else pd.DataFrame()
    meta_row = pd.read_sql("SELECT portfolio_total_volume FROM batch_meta ORDER BY id DESC LIMIT 1", conn)
    conn.close()

    portfolio_total = float(meta_row["portfolio_total_volume"].iloc[0]) if len(meta_row) and pd.notna(meta_row["portfolio_total_volume"].iloc[0]) else None

    if manifest_df.empty:
        print("    (nothing in db yet, skipping CSV rebuild)")
        return

    tracker_rows = []
    for _, m in manifest_df.iterrows():
        qshare = (float(m["total_volume"]) / portfolio_total * 100) if portfolio_total else None
        sku_forecasts = forecasts_df[forecasts_df["sku"] == m["sku"]]
        if sku_forecasts.empty:
            for h_days, h_label in HORIZON_LABELS.items():
                tracker_rows.append({
                    "SKU": m["sku"], "Description": m["description"], "Family": m["family"],
                    "ADI": m["adi"], "CV2": m["cv2"], "Demand_Pattern": m["demand_pattern"],
                    "Zero_Rate": m["zero_pct"], "Quantum_Share_Pct": round(qshare, 4) if qshare is not None else None,
                    "Horizon": h_label, "Model": None, "WAPE": None,
                    "Grade": "Insufficient History" if not m["is_forecastable"] else "No Model",
                })
        else:
            for _, row in sku_forecasts.iterrows():
                tracker_rows.append({
                    "SKU": m["sku"], "Description": m["description"], "Family": m["family"],
                    "ADI": m["adi"], "CV2": m["cv2"], "Demand_Pattern": m["demand_pattern"],
                    "Zero_Rate": m["zero_pct"], "Quantum_Share_Pct": round(qshare, 4) if qshare is not None else None,
                    "Horizon": HORIZON_LABELS.get(row["horizon_days"], row["horizon_days"]),
                    "Model": row["best_model"], "WAPE": row["wape"], "Grade": row["grade"],
                })

    tracker_df = pd.DataFrame(tracker_rows).merge(
        manifest_df[["sku", "velocity_tier"]].rename(columns={"sku": "SKU", "velocity_tier": "Velocity"}),
        on="SKU", how="left",
    )
    ok1 = _safe_to_csv(tracker_df, tracker_csv_out, "tracker CSV")

    all_forecast_rows = forecasts_df.to_dict("records")
    matrix_df = build_manager_matrix(all_forecast_rows)
    ok2 = _safe_to_csv(matrix_df, matrix_csv_out, "matrix CSV")

    if not grid_df.empty:
        ok3 = _safe_to_csv(grid_df, grid_csv_out, "full grid CSV")
    else:
        ok3 = True
        print("    (no grid data accumulated yet)")

    if not daily_df.empty:
        daily_export = daily_df.copy()
        daily_export["Horizon"] = daily_export["horizon_days"].map(DAILY_HORIZON_LABELS)
        ok4 = _safe_to_csv(daily_export, daily_csv_out, "daily (2D/3D) CSV")
    else:
        ok4 = True
        print("    (no daily 2D/3D data accumulated yet)")

    if ok1:
        print(f"    Tracker CSV rebuilt from {manifest_df['sku'].nunique()} accumulated SKUs -> {tracker_csv_out}")
    if ok2:
        print(f"    Matrix CSV rebuilt  -> {matrix_csv_out}")
    if ok3 and not grid_df.empty:
        print(f"    Full grid CSV rebuilt ({len(grid_df):,} rows) -> {grid_csv_out}")
    if ok4 and not daily_df.empty:
        print(f"    Daily (2D/3D) CSV rebuilt ({len(daily_df):,} rows) -> {daily_csv_out}")


# ── MAIN BATCH ────────────────────────────────────────────────────────────────

# ── INVENTORY (WES pallet snapshot, full replace every run) ─────────────────
# Folded in here rather than kept as a separate script, since inventory is
# a point-in-time snapshot with no installment/resume concept - every batch
# run re-ingests whatever snapshot is present and fully replaces
# sku_inventory, regardless of which forecasting installment is running.
#
# LIMITATION, surfaced rather than hidden: the WES pallet export has no
# "allocated" column - it's physical stock only. allocated is written as 0
# and available = on_hand, labelled via source_tag so the gap number is
# never mistaken for netting out committed orders it never saw. Wire in a
# real allocation source (WES fetch API, order_container_mapping) before
# treating "available" as more than physical on-hand.

def ingest_inventory(conn: sqlite3.Connection, inventory_path: str) -> int:
    if not inventory_path or not os.path.exists(inventory_path):
        print(f"    (no inventory snapshot at {inventory_path}, skipping - sku_inventory left as-is)")
        return 0

    inv_raw = pd.read_csv(inventory_path)
    inv_raw["SKU ID"] = inv_raw["SKU ID"].astype(str)
    agg = inv_raw.groupby("SKU ID").agg(
        on_hand_kg=("Weight", "sum"), n_pallets=("Pallet ID", "count"),
    ).reset_index().rename(columns={"SKU ID": "sku"})
    agg["on_hand"] = agg["on_hand_kg"] / 1000.0
    agg["allocated"] = 0.0
    agg["available"] = agg["on_hand"] - agg["allocated"]
    agg["source_tag"] = "WES_pallet_snapshot_on_hand_only"
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn.execute("""CREATE TABLE IF NOT EXISTS sku_inventory (
        sku TEXT PRIMARY KEY, on_hand REAL, allocated REAL, available REAL,
        n_pallets INTEGER, source_tag TEXT, updated_at TEXT)""")
    conn.execute("DELETE FROM sku_inventory")   # full replace, snapshot has no installment concept
    for _, r in agg.iterrows():
        conn.execute("INSERT INTO sku_inventory VALUES (?,?,?,?,?,?,?)", (
            r["sku"], round(r["on_hand"], 3), round(r["allocated"], 3), round(r["available"], 3),
            int(r["n_pallets"]), r["source_tag"], updated_at,
        ))
    conn.commit()
    return len(agg)


# ── MAIN BATCH ────────────────────────────────────────────────────────────────

def run_batch(input_path: str, db_out: str, tracker_csv_out: str, matrix_csv_out: str,
              run_type: str = "manual", batch_size: int = 50, export_only: bool = False,
              grid_csv_out: str = "petmaxi_full_grid_v7.csv",
              daily_csv_out: str = "petmaxi_daily_forecasts_v7.csv",
              inventory_path: str = None) -> None:
    t0 = datetime.now()
    run_at = t0.strftime("%Y-%m-%d %H:%M")
    print("=" * 65)
    print("  PetMaxi Batch Forecast Job — v7 (weekly + daily 2D/3D)")
    print(f"  {run_at}")
    print("=" * 65)

    # ── [0] Inventory snapshot (full replace, every invocation) ──────────
    # Runs regardless of export_only or forecasting progress - it's a
    # point-in-time snapshot, not tied to which SKUs have been batched yet.
    if inventory_path:
        print(f"\n[0] Ingesting inventory snapshot: {inventory_path}")
        conn_inv = sqlite3.connect(db_out)
        n_inv = ingest_inventory(conn_inv, inventory_path)
        conn_inv.close()
        if n_inv:
            print(f"    {n_inv} SKUs' inventory refreshed in {db_out}.")
    else:
        print("\n[0] No --inventory-input given, sku_inventory left as-is.")

    if export_only:
        print("\n[export-only] Rebuilding CSVs from current db state, no new SKUs processed.")
        rebuild_csvs_from_db(db_out, tracker_csv_out, matrix_csv_out, grid_csv_out, daily_csv_out)
        return

    # ── [1] Data prep (weekly) ───────────────────────────────────────────
    print(f"\n[1] Data prep from raw: {input_path}")
    weekly_clean = prep.run_prep(
        input_path=input_path,
        out_xlsx="_v7_prepared_intermediate.xlsx",
        out_csv="_v7_weekly_intermediate.csv",
        log_csv="_v7_prep_log.csv",
    )
    raw = prep.load_raw(input_path)
    recovered = prep.recover_qtton_from_qtsac(raw["SALES"], raw[prep.FAMILY_SHEET])
    scoped = prep.filter_scope(recovered)
    meta_lookup = (scoped.groupby("SKU")
                   .agg(description=("Description", "first"), family=("Family", "first"))
                   .reset_index())

    total_volume_lookup = weekly_clean.groupby("SKU")["qtton"].sum()
    portfolio_total_volume = total_volume_lookup.sum()

    # ── [1b] Daily series (2D/3D), reusing the same scoped frame ────────
    print(f"\n[1b] Building daily (2D/3D) series from the same scoped data ...")
    daily_series_by_sku, daily_max_date = build_daily_series(scoped)
    print(f"    Daily series built for {len(daily_series_by_sku)} SKUs, last actual day: {daily_max_date.date()}")

    # ── [2] Auto-resume: figure out which SKUs are left ─────────────────
    all_skus = list(weekly_clean["SKU"].unique())
    already_done = get_already_processed_skus(db_out)
    remaining = [s for s in all_skus if s not in already_done]

    print(f"\n[2] {len(already_done)} SKUs already processed in {db_out}, "
          f"{len(remaining)} remaining out of {len(all_skus)} total.")

    if not remaining:
        print("    Nothing left to process. Rebuilding CSVs from final state and exiting.")
        rebuild_csvs_from_db(db_out, tracker_csv_out, matrix_csv_out, grid_csv_out, daily_csv_out)
        return

    batch_skus = remaining[:batch_size]
    print(f"    Processing this installment: {len(batch_skus)} SKUs "
          f"({len(remaining) - len(batch_skus)} will remain after this run)")

    # ── [3] Gap-bridging anchors ──────────────────────────────────────────
    # Weekly: last real week in weekly_clean vs a real "today", rounded
    # to next Monday (existing display convention). Daily: last real day
    # in the daily series vs a real "tomorrow". Forecasts always display
    # from the real batch-run date; the model bridges whatever gap exists
    # to its own last real data point via forecasting_engine_v7's
    # gap_periods mechanism, see that module's docstring fix #6.
    weekly_series_end = weekly_clean["week"].max()
    start_date = pd.Timestamp.today().normalize() + pd.offsets.Week(weekday=0)
    gap_weeks = max(0, int((start_date - weekly_series_end) / pd.Timedelta(weeks=1)))
    start_date_str = start_date.strftime("%Y-%m-%d")

    daily_start_date = pd.Timestamp.today().normalize() + pd.Timedelta(days=1)
    gap_days = max(0, int((daily_start_date - daily_max_date) / pd.Timedelta(days=1)))
    daily_start_date_str = daily_start_date.strftime("%Y-%m-%d")

    print(f"\n[3] Gap-bridging: weekly data ends {weekly_series_end.date()} "
          f"-> bridging {gap_weeks} week(s) before the displayed horizon begins.")
    print(f"    Daily data ends {daily_max_date.date()} "
          f"-> bridging {gap_days} day(s) before the displayed horizon begins.")

    manifest_rows, all_forecast_rows, all_grid_rows = [], [], []
    all_daily_forecast_rows, all_daily_grid_rows = [], []

    n_workers = max(1, (os.cpu_count() or 4) - 1)
    print(f"    Parallel workers: {n_workers} (leaves 1 core free)")

    tasks = []
    for sku in batch_skus:
        weekly_vals = weekly_clean.loc[weekly_clean["SKU"] == sku, "qtton"].values.astype(float).tolist()
        daily_vals = daily_series_by_sku.get(sku)
        daily_vals_list = daily_vals.tolist() if daily_vals is not None else None
        tasks.append((sku, weekly_vals, start_date_str, gap_weeks,
                      daily_vals_list, daily_start_date_str, gap_days))

    done_count = 0
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_process_one_sku_worker, t): t[0] for t in tasks}
        for future in as_completed(futures):
            sku = futures[future]
            try:
                sku_out, result = future.result()
            except Exception as e:
                print(f"    WARN: worker failed for {sku}: {e}")
                continue

            manifest_rows.append({"sku": sku_out, "total_volume": float(np.sum(
                weekly_clean.loc[weekly_clean["SKU"] == sku_out, "qtton"].values)),
                "n_active": result["manifest"]["n_active"], **result["manifest"]})

            for row in result["forecasts"]:
                all_forecast_rows.append({**row, "run_at": run_at})
            for row in result.get("full_grid", []):
                all_grid_rows.append({**row, "run_at": run_at})
            for row in result.get("daily_forecasts", []):
                all_daily_forecast_rows.append({**row, "run_at": run_at})
            for row in result.get("daily_grid", []):
                all_daily_grid_rows.append({**row, "run_at": run_at})

            done_count += 1
            if done_count % 25 == 0:
                print(f"    {done_count}/{len(batch_skus)} done in this installment ...")

    print(f"    Weekly forecast rows generated this installment: {len(all_forecast_rows)}")
    print(f"    Weekly full grid rows generated this installment: {len(all_grid_rows)}")
    print(f"    Daily (2D/3D) forecast rows generated this installment: {len(all_daily_forecast_rows)}")

    manifest_df = pd.DataFrame(manifest_rows)
    n_forecastable = int(manifest_df["is_forecastable"].sum())
    # NOTE: velocity_tier for THIS installment is intentionally not computed
    # here on manifest_df alone - percentiles need the full accumulated
    # portfolio as the denominator (see the full recompute pass after the
    # db write below), not just this installment's ~50 SKUs, or a SKU's
    # tier ends up depending on which batch slice it happened to land in.

    # ── [4] SQLite db, per-SKU upsert (does NOT wipe other installments) ─
    print(f"\n[4] Writing this installment's results to db -> {db_out}")
    conn = sqlite3.connect(db_out)
    conn.execute("""CREATE TABLE IF NOT EXISTS batch_meta (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, data_file TEXT,
        total_skus INTEGER, forecastable INTEGER, run_type TEXT,
        status TEXT, duration_sec REAL, portfolio_total_volume REAL,
        gap_weeks INTEGER, gap_days INTEGER,
        weekly_data_as_of TEXT, daily_data_as_of TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sku_manifest (
        sku TEXT PRIMARY KEY, description TEXT, family TEXT,
        velocity_tier TEXT, total_volume REAL, n_active INTEGER,
        is_forecastable INTEGER, status TEXT, demand_pattern TEXT,
        adi REAL, cv2 REAL, zero_pct REAL, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sku_forecasts (
        sku TEXT, horizon_days INTEGER, horizon_weeks INTEGER,
        best_model TEXT, wape REAL, robust_mape REAL, forecast_total REAL,
        forecast_values TEXT, forecast_dates TEXT, grade TEXT,
        zero_wape_override INTEGER, untrusted_zero_model TEXT,
        data_gap_periods INTEGER, run_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sku_grid (
        sku TEXT, horizon_days INTEGER, horizon_weeks INTEGER,
        model TEXT, wape REAL, robust_mape REAL, run_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sku_daily_forecasts (
        sku TEXT, horizon_days INTEGER, best_model TEXT, wape REAL,
        forecast_total REAL, forecast_values TEXT, forecast_dates TEXT,
        grade TEXT, zero_wape_override INTEGER, untrusted_zero_model TEXT,
        data_gap_periods INTEGER, run_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sku_daily_grid (
        sku TEXT, horizon_days INTEGER, model TEXT, wape REAL,
        robust_mape REAL, run_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sku_daily_manifest (
        sku TEXT PRIMARY KEY, n_days_history INTEGER, updated_at TEXT)""")

    for _, r in manifest_df.iterrows():
        desc_row = meta_lookup[meta_lookup["SKU"] == r["sku"]]
        description = desc_row["description"].iloc[0] if len(desc_row) else ""
        family = desc_row["family"].iloc[0] if len(desc_row) else ""
        conn.execute("DELETE FROM sku_manifest WHERE sku = ?", (r["sku"],))
        conn.execute("""INSERT INTO sku_manifest VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            r["sku"], description, family, None,  # velocity_tier: set below by the
                                                    # full-portfolio recompute pass,
                                                    # not from this installment alone
            float(r.get("total_volume", 0)), int(r.get("n_active", 0)),
            int(bool(r.get("is_forecastable"))), r.get("status"),
            r.get("demand_pattern"), r.get("adi"), r.get("cv2"),
            r.get("zero_pct"), run_at,
        ))

    for row in all_forecast_rows:
        conn.execute("DELETE FROM sku_forecasts WHERE sku = ? AND horizon_days = ?",
                     (row["sku"], row["horizon_days"]))
        conn.execute("""INSERT INTO sku_forecasts VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            row["sku"], row["horizon_days"], row["horizon_weeks"], row["best_model"],
            row["wape"], row["robust_mape"], row["forecast_total"],
            row["forecast_values"], row["forecast_dates"], row["grade"],
            int(bool(row.get("zero_wape_override", False))), row.get("untrusted_zero_model"),
            row.get("data_gap_periods"), row["run_at"],
        ))

    for sku in batch_skus:
        conn.execute("DELETE FROM sku_grid WHERE sku = ?", (sku,))
        conn.execute("DELETE FROM sku_daily_forecasts WHERE sku = ?", (sku,))
        conn.execute("DELETE FROM sku_daily_grid WHERE sku = ?", (sku,))
    for row in all_grid_rows:
        conn.execute("""INSERT INTO sku_grid VALUES (?,?,?,?,?,?,?)""", (
            row["sku"], row["horizon_days"], row["horizon_weeks"],
            row["model"], row["wape"], row["robust_mape"], row["run_at"],
        ))
    for row in all_daily_forecast_rows:
        conn.execute("""INSERT INTO sku_daily_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            row["sku"], row["horizon_days"], row["best_model"], row["wape"],
            row["forecast_total"], row["forecast_values"], row["forecast_dates"], row["grade"],
            int(bool(row.get("zero_wape_override", False))), row.get("untrusted_zero_model"),
            row.get("data_gap_periods"), row["run_at"],
        ))
    for row in all_daily_grid_rows:
        conn.execute("""INSERT INTO sku_daily_grid VALUES (?,?,?,?,?,?)""", (
            row["sku"], row["horizon_days"], row["model"], row["wape"],
            row["robust_mape"], row["run_at"],
        ))
    for sku in batch_skus:
        n_days = len(daily_series_by_sku[sku]) if sku in daily_series_by_sku else 0
        conn.execute("DELETE FROM sku_daily_manifest WHERE sku = ?", (sku,))
        conn.execute("INSERT INTO sku_daily_manifest VALUES (?,?,?)", (sku, n_days, run_at))

    # ── [4b] Recompute ABC tiers against the FULL accumulated portfolio ──
    # Percentile cutoffs need every SKU processed so far as the denominator,
    # not just this installment's ~50 SKUs - otherwise a SKU's tier depends
    # on which batch slice it happened to land in. Re-read everything back
    # out, recompute, and update every row, every installment.
    full_manifest = pd.read_sql("SELECT sku, total_volume, n_active FROM sku_manifest", conn)
    full_recomputed = compute_abc_tiers(full_manifest)
    for _, r in full_recomputed.iterrows():
        conn.execute("UPDATE sku_manifest SET velocity_tier = ? WHERE sku = ?",
                     (r["velocity_tier"], r["sku"]))
    manifest_df = manifest_df.merge(
        full_recomputed[["sku", "velocity_tier"]], on="sku", how="left")
    print(f"    ABC tiers recomputed against all {len(full_recomputed)} SKUs processed so far "
          f"(not just this installment).")

    duration = (datetime.now() - t0).total_seconds()
    conn.execute("""INSERT INTO batch_meta
        (run_at, data_file, total_skus, forecastable, run_type, status, duration_sec,
         portfolio_total_volume, gap_weeks, gap_days, weekly_data_as_of, daily_data_as_of)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_at, input_path, len(batch_skus), n_forecastable, run_type,
                 f"INSTALLMENT ({len(remaining)-len(batch_skus)} left)", duration,
                 float(portfolio_total_volume), gap_weeks, gap_days,
                 weekly_series_end.strftime("%Y-%m-%d"), daily_max_date.strftime("%Y-%m-%d")))
    conn.commit()
    conn.close()

    for f in ("_v7_prepared_intermediate.xlsx", "_v7_weekly_intermediate.csv", "_v7_prep_log.csv"):
        if os.path.exists(f):
            os.remove(f)

    # ── [5] Rebuild all CSVs from the FULL accumulated db state ─────────
    print(f"\n[5] Rebuilding tracker + matrix + daily CSVs from full accumulated progress ...")
    rebuild_csvs_from_db(db_out, tracker_csv_out, matrix_csv_out, grid_csv_out, daily_csv_out)

    print(f"\nINSTALLMENT COMPLETE in {duration:.1f}s")
    print(f"  Processed this run : {len(batch_skus)} SKUs")
    print(f"  Remaining          : {len(remaining) - len(batch_skus)} SKUs")
    print(f"  Rerun the same command to continue with the next installment.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PetMaxi batch v7 (weekly + daily 2D/3D)")
    parser.add_argument("--input",   required=True, help="raw vendas xlsx path")
    parser.add_argument("--db-out",  default="petmaxi_v7.db")
    parser.add_argument("--inventory-input", default=None,
                        help="WES pallet inventory snapshot CSV, refreshed (full replace) every run regardless of forecasting progress")
    parser.add_argument("--tracker-out", default="petmaxi_tracker_v7.csv")
    parser.add_argument("--matrix-out",  default="petmaxi_manager_matrix_v7.csv")
    parser.add_argument("--grid-out",    default="petmaxi_full_grid_v7.csv",
                        help="every model x horizon result per SKU (weekly), full transparency export")
    parser.add_argument("--daily-out",   default="petmaxi_daily_forecasts_v7.csv",
                        help="2D/3D forecast rows per SKU")
    parser.add_argument("--run-type", default="manual", choices=["manual", "delta_adjustment"])
    parser.add_argument("--batch-size", type=int, default=50,
                        help="how many new SKUs to process this run, rerun the same command to continue")
    parser.add_argument("--export-only", action="store_true",
                        help="skip processing, just rebuild CSVs from the db's current state")
    args = parser.parse_args()

    try:
        run_batch(args.input, args.db_out, args.tracker_out, args.matrix_out,
                  args.run_type, args.batch_size, args.export_only, args.grid_out, args.daily_out,
                  args.inventory_input)
    except Exception as e:
        print(f"\nERROR: batch failed: {e}")
        sys.exit(1)
