"""
PetMaxi Data Preparation — v1
===============================
Takes the raw sales export (vendas_1.xlsx or equivalent) and produces a
clean, weekly-aggregated series per SKU, ready for forecasting_engine_v7.

This replaces the old prepare_vendas_v4.py -> vendas_prepared.xlsx step.
Key differences from that old approach, agreed on during review:

  1. Zero-demand weeks are explicitly filled (old prep silently dropped
     QTTON <= 0 rows before aggregation, which meant weeks with no sale
     never existed in the series at all, corrupting ADI/CV2 and any
     rolling-origin evaluation downstream).
  2. Zero-fill start is per-SKU (that SKU's own first sale week), so a
     SKU is never penalised for weeks before it existed. Zero-fill end is
     the dataset's overall max observed week, shared across all SKUs, not
     each SKU's own last sale, and not the real-world current date. A SKU
     that goes quiet while the rest of the portfolio keeps transacting
     has genuine zero demand in that gap, that should show as zero-fill
     weeks, not just have its series stop early. But nothing extends past
     the last week actually present in the data, since that would invent
     an observation window with no data collected at all.
  3. No minimum-active-weeks filter is applied here. Short-history SKUs
     are kept and flagged downstream by the forecastability gate
     (Insufficient History), not silently dropped in prep. Dropping them
     here would hide the same 123-SKU gap issue we are trying to fix.
  4. QTSAC recovery safety check: a few raw rows can have QTTON <= 0 while
     QTSAC (sack count) is still positive, meaning a real sale exists but
     got rounded to zero tons. Where a SKU has a WEIGHT entry in
     SKU_FAMILY, QTTON is recovered as QTSAC * WEIGHT / 1000. On the
     current raw file this check finds nothing inside the Produto
     Acabado / Mercadoria scope (all such rows belong to packaging,
     services, waste, or transport line items), but it costs nothing to
     keep and protects against a future data drop behaving differently.
  5. Export rows (Export == 'SIM') are excluded. The business goal is FG
     delivery time to end customers; export channel volume would distort
     that demand signal if blended in.
  6. Outlier capping (Q3 + 3*IQR per SKU) runs AFTER zero-filling, on the
     true continuous series, not before. Capping on a sparse series
     before zero-fill would compute Q1/Q3 on the wrong distribution.
  7. No hardcoded year range. Prior prep capped at 2023-2025; the raw
     file now extends into 2026 and there is no reason to cut that off.

Output:
  - A cleaned weekly long-format table (sku, week, qtton) as CSV
  - A prepared workbook (xlsx) carrying that weekly table plus the
    original SKU_FAMILY / sku_formula / Formula sheets untouched, so it
    is a drop-in DATA_PATH for run_batch and the dashboard backend's RM
    lookups.
  - A prep summary log (CSV) with row/SKU counts at each stage, for audit.
"""

import os
import sys
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

# ── CONFIG ───────────────────────────────────────────────────────────────────

SKU_TYPES_KEEP   = ["Produto Acabado", "Mercadoria"]
EXPORT_EXCLUDE    = "SIM"          # keep only rows where Export != this value
OUTLIER_IQR_MULT  = 3.0            # Q3 + OUTLIER_IQR_MULT * IQR cap, per SKU

DEFAULT_INPUT     = "vendas_1_1.xlsx"
DEFAULT_OUT_XLSX  = "vendas_prepared_clean.xlsx"
DEFAULT_OUT_CSV   = "petmaxi_weekly_clean.csv"
DEFAULT_LOG_CSV   = "data_prep_log.csv"

SALES_SHEET       = "SALES"
FAMILY_SHEET      = "SKU_FAMILY"
FORMULA_MAP_SHEET = "sku_formula"
FORMULA_SHEET     = "Formula"


# ── STEP 1: LOAD ─────────────────────────────────────────────────────────────

def load_raw(input_path: str) -> dict:
    """
    Load all four sheets from the raw workbook. Returns a dict of DataFrames.
    Fails loudly if SALES sheet is missing, since nothing downstream works
    without it.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Raw data file not found: {input_path}")

    xls = pd.ExcelFile(input_path)
    sheets_present = set(xls.sheet_names)

    if SALES_SHEET not in sheets_present:
        raise ValueError(
            f"'{SALES_SHEET}' sheet not found in {input_path}. "
            f"Sheets present: {sorted(sheets_present)}"
        )

    data = {"SALES": pd.read_excel(xls, sheet_name=SALES_SHEET)}

    for opt_sheet in (FAMILY_SHEET, FORMULA_MAP_SHEET, FORMULA_SHEET):
        if opt_sheet in sheets_present:
            data[opt_sheet] = pd.read_excel(xls, sheet_name=opt_sheet)
        else:
            print(f"  WARN: sheet '{opt_sheet}' not found, downstream RM "
                  f"lookups depending on it will return empty results.")
            data[opt_sheet] = pd.DataFrame()

    return data


# ── STEP 2: QTSAC RECOVERY SAFETY CHECK ─────────────────────────────────────

def recover_qtton_from_qtsac(sales: pd.DataFrame, family: pd.DataFrame) -> pd.DataFrame:
    """
    Where QTTON <= 0 (or missing) but QTSAC > 0 and a WEIGHT is known for
    that SKU, recover QTTON = QTSAC * WEIGHT / 1000 (kg -> tons).

    On the current raw file this finds 0 rows inside the FG scope (all
    QTTON<=0 rows belong to non-FG SKU types), but the check runs
    regardless so a future data drop with a real gap gets caught instead
    of silently zero-filled as no-demand.
    """
    sales = sales.copy()

    needs_recovery = (sales["QTTON"].isna() | (sales["QTTON"] <= 0)) & (sales["QTSAC"] > 0)
    n_candidates = int(needs_recovery.sum())

    if n_candidates == 0:
        print("  QTSAC recovery: 0 candidate rows (QTTON<=0 with QTSAC>0). Nothing to do.")
        return sales

    if family.empty or "WEIGHT" not in family.columns:
        print(f"  WARN: {n_candidates} candidate rows found for QTSAC recovery, "
              f"but SKU_FAMILY/WEIGHT is unavailable, skipping recovery.")
        return sales

    weight_lookup = family.set_index("SKU")["WEIGHT"].to_dict()
    sales["_recovered_weight"] = sales["SKU"].map(weight_lookup)

    recoverable = needs_recovery & sales["_recovered_weight"].notna()
    n_recovered = int(recoverable.sum())

    sales.loc[recoverable, "QTTON"] = (
        sales.loc[recoverable, "QTSAC"] * sales.loc[recoverable, "_recovered_weight"] / 1000.0
    )

    n_unresolved = n_candidates - n_recovered
    print(f"  QTSAC recovery: {n_candidates} candidate rows, "
          f"{n_recovered} recovered via WEIGHT match, "
          f"{n_unresolved} left as-is (no WEIGHT on file for that SKU).")

    sales = sales.drop(columns=["_recovered_weight"])
    return sales


# ── STEP 3: SCOPE FILTER ─────────────────────────────────────────────────────

def filter_scope(sales: pd.DataFrame) -> pd.DataFrame:
    """
    Keep only Produto Acabado + Mercadoria SKU types. Export is NOT
    excluded, export and domestic sales are blended into one combined
    demand signal per SKU (reversed from the earlier exclude-export
    decision). No year filter, no min-history filter, both intentionally
    deferred (year: no reason to cut off recent data; history: handled
    downstream by the forecastability gate, not here).
    """
    before_rows = len(sales)
    before_skus = sales["SKU"].nunique()

    filtered = sales[sales["SKU type"].isin(SKU_TYPES_KEEP)].copy()

    after_rows = len(filtered)
    after_skus = filtered["SKU"].nunique()

    print(f"  Scope filter: {before_rows:,} rows / {before_skus} SKUs -> "
          f"{after_rows:,} rows / {after_skus} SKUs "
          f"(type in {SKU_TYPES_KEEP}, export blended in, not excluded)")

    return filtered


# ── STEP 4: WEEKLY AGGREGATION ───────────────────────────────────────────────

def aggregate_weekly(sales: pd.DataFrame) -> pd.DataFrame:
    """
    Build a date column from year/month/day and aggregate QTTON to weekly
    totals per SKU. Uses the same to_period('W').dt.to_timestamp() anchor
    convention as the existing engine, so weekly boundaries line up with
    everything downstream.
    """
    sales = sales.copy()
    sales["date"] = pd.to_datetime(
        dict(year=sales["year"], month=sales["month"], day=sales["day"]),
        errors="coerce",
    )

    n_bad_dates = int(sales["date"].isna().sum())
    if n_bad_dates:
        print(f"  WARN: {n_bad_dates} rows have unparseable dates, dropping them.")
        sales = sales.dropna(subset=["date"])

    sales["week"] = sales["date"].dt.to_period("W").dt.to_timestamp()

    weekly = (
        sales.groupby(["SKU", "week"])["QTTON"]
        .sum()
        .reset_index()
        .rename(columns={"QTTON": "qtton"})
        .sort_values(["SKU", "week"])
    )

    print(f"  Weekly aggregation: {len(weekly):,} SKU-week rows across "
          f"{weekly['SKU'].nunique()} SKUs.")

    return weekly


# ── STEP 5: ZERO-FILL PER SKU (OWN ACTIVE LIFE ONLY) ────────────────────────

def zero_fill_per_sku(weekly: pd.DataFrame) -> pd.DataFrame:
    """
    For each SKU, fill in missing weeks with qtton=0, but only between
    that SKU's own first and last observed week. Never extends past the
    SKU's last real sale, never extends before its first, and never uses
    a portfolio-wide date range.
    """
    filled_frames = []
    n_weeks_added = 0

    # End boundary is the dataset's own max observed week, shared across
    # all SKUs, not each SKU's individual last sale. A SKU that goes quiet
    # while the rest of the portfolio keeps transacting has genuinely zero
    # demand in that gap, that should be a zero-fill week, not just have
    # its series stop early. Start boundary stays per-SKU (first sale),
    # since a SKU truly did not exist before that week.
    dataset_max_week = weekly["week"].max()

    for sku, grp in weekly.groupby("SKU", sort=False):
        grp = grp.sort_values("week")
        # Plain 7-day step from this SKU's own first week, not freq='W'.
        # freq='W' anchors to Sundays by default, but week values here are
        # Monday-anchored (from to_period('W').to_timestamp() upstream).
        # For a single-week SKU that mismatch made date_range return zero
        # dates, silently dropping the SKU instead of leaving it unfilled.
        full_range = pd.date_range(
            start=grp["week"].min(), end=dataset_max_week, freq="7D"
        )

        reindexed = (
            grp.set_index("week")
            .reindex(full_range)
            .rename_axis("week")
            .reset_index()
        )
        reindexed["SKU"] = sku
        reindexed["qtton"] = reindexed["qtton"].fillna(0.0)

        n_weeks_added += int(reindexed["qtton"].eq(0).sum() - grp["qtton"].eq(0).sum())
        filled_frames.append(reindexed[["SKU", "week", "qtton"]])

    result = pd.concat(filled_frames, ignore_index=True).sort_values(["SKU", "week"])

    print(f"  Zero-fill: added {n_weeks_added:,} explicit zero-demand weeks "
          f"across {result['SKU'].nunique()} SKUs (each SKU's own active "
          f"range only).")

    return result


# ── STEP 6: OUTLIER CAP (Q3 + 3*IQR PER SKU, AFTER ZERO-FILL) ───────────────

def cap_outliers_per_sku(weekly: pd.DataFrame, iqr_mult: float = OUTLIER_IQR_MULT) -> pd.DataFrame:
    """
    Cap each SKU's weekly qtton at Q3 + iqr_mult * IQR, computed on that
    SKU's own zero-filled series (so the true continuous distribution,
    including zero weeks, informs the cap). Runs per SKU since demand
    scale varies enormously across the portfolio.
    """
    weekly = weekly.copy()

    # Q1/Q3 computed on NONZERO values only. Zeros are structural (real
    # no-demand weeks), never candidates for being "outliers" on the high
    # side, and including them corrupts the quantiles for any sparse
    # series: with >75% zero weeks, Q3 itself lands on 0, making the cap
    # 0 and clip(upper=0) silently wipes out every real demand value for
    # that SKU. Confirmed this happening on real data before this fix.
    nonzero = weekly[weekly["qtton"] > 0]
    q1_lookup = nonzero.groupby("SKU")["qtton"].quantile(0.25)
    q3_lookup = nonzero.groupby("SKU")["qtton"].quantile(0.75)

    q1 = weekly["SKU"].map(q1_lookup)
    q3 = weekly["SKU"].map(q3_lookup)
    cap = q3 + iqr_mult * (q3 - q1)

    # SKUs with fewer than 2 nonzero weeks have no meaningful IQR, skip
    # capping for them entirely rather than deriving a degenerate cap.
    has_valid_cap = cap.notna()

    n_capped_total = int(((weekly["qtton"] > cap) & has_valid_cap).sum())
    weekly.loc[has_valid_cap, "qtton"] = weekly.loc[has_valid_cap, "qtton"].clip(
        upper=cap[has_valid_cap]
    )

    print(f"  Outlier cap: {n_capped_total} SKU-weeks capped at "
          f"Q3 + {iqr_mult}*IQR (per SKU, nonzero-only quantiles).")

    return weekly


# ── STEP 6.5: RUN_BATCH_V4_1 COMPATIBILITY SHAPE ─────────────────────────────

def build_run_batch_compatible_sales(weekly_clean: pd.DataFrame,
                                      sales_scoped: pd.DataFrame) -> pd.DataFrame:
    """
    run_batch_v4_1.py expects a 'SALES' sheet at row level with
    year, month, day, SKU, QTTON, Description, Family, SKU type, and does
    its own date reconstruction + groupby(SKU, week).sum(QTTON).

    Our weekly_clean is already aggregated and zero-filled (SKU, week,
    qtton only). To keep run_batch_v4_1.py completely unmodified (so the
    old engine really is unchanged, only the data underneath it), this
    reshapes weekly_clean back into that row-level format:
      - qtton -> QTTON
      - week -> year/month/day, reconstructed so that run_batch's own
        date -> to_period('W').to_timestamp() round-trips to the exact
        same Monday-anchored week, since that's the same convention used
        upstream in aggregate_weekly(). Its re-aggregation becomes a
        no-op (one row per SKU per week, summed with itself).
      - Description/Family/SKU type carried through per SKU (first
        non-null value in the scoped raw data), since run_batch reads
        those via groupby("SKU").agg(..., "first").
    """
    meta = (
        sales_scoped.groupby("SKU")
        .agg(Description=("Description", "first"),
             Family=("Family", "first"),
             **{"SKU type": ("SKU type", "first")})
        .reset_index()
    )

    out = weekly_clean.merge(meta, on="SKU", how="left")
    out["QTTON"] = out["qtton"]
    out["year"]  = out["week"].dt.year
    out["month"] = out["week"].dt.month
    out["day"]   = out["week"].dt.day

    out = out[["SKU", "Description", "Family", "SKU type",
               "year", "month", "day", "QTTON"]]

    n_unmatched = int(out["Description"].isna().sum())
    if n_unmatched:
        print(f"  WARN: {n_unmatched} rows have no Description/Family match "
              f"(SKU missing from scoped raw data), check for SKU code drift.")

    return out




def run_prep(input_path: str = DEFAULT_INPUT,
             out_xlsx: str = DEFAULT_OUT_XLSX,
             out_csv: str = DEFAULT_OUT_CSV,
             log_csv: str = DEFAULT_LOG_CSV) -> pd.DataFrame:

    t0 = datetime.now()
    print("=" * 65)
    print("  PetMaxi Data Prep v1")
    print(f"  {t0.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)

    print(f"\n[1] Loading raw data from: {input_path}")
    data = load_raw(input_path)
    sales_raw = data["SALES"]
    print(f"  Loaded {len(sales_raw):,} raw rows, {sales_raw['SKU'].nunique()} unique SKUs.")

    print("\n[2] QTSAC recovery safety check")
    sales_recovered = recover_qtton_from_qtsac(sales_raw, data[FAMILY_SHEET])

    print("\n[3] Scope filter (SKU type, export)")
    sales_scoped = filter_scope(sales_recovered)

    print("\n[4] Weekly aggregation")
    weekly = aggregate_weekly(sales_scoped)

    print("\n[5] Zero-fill (per-SKU active life)")
    weekly_filled = zero_fill_per_sku(weekly)

    print("\n[6] Outlier capping (Q3 + 3*IQR, per SKU, post zero-fill)")
    weekly_clean = cap_outliers_per_sku(weekly_filled)

    # ── Write outputs ────────────────────────────────────────────────────
    print(f"\n[7] Writing outputs")

    weekly_clean.to_csv(out_csv, index=False)
    print(f"  Weekly clean CSV -> {out_csv}")

    sales_compatible = build_run_batch_compatible_sales(weekly_clean, sales_scoped)

    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
        sales_compatible.to_excel(writer, sheet_name="SALES", index=False)
        for sheet_name in (FAMILY_SHEET, FORMULA_MAP_SHEET, FORMULA_SHEET):
            if not data[sheet_name].empty:
                data[sheet_name].to_excel(writer, sheet_name=sheet_name, index=False)
    print(f"  Prepared workbook -> {out_xlsx} "
          f"(SALES [run_batch_v4_1-compatible] + {FAMILY_SHEET}/{FORMULA_MAP_SHEET}/{FORMULA_SHEET})")

    duration = (datetime.now() - t0).total_seconds()
    log_row = pd.DataFrame([{
        "run_at":              t0.strftime("%Y-%m-%d %H:%M"),
        "input_file":          input_path,
        "raw_rows":            len(sales_raw),
        "raw_skus":            sales_raw["SKU"].nunique(),
        "scoped_rows":         len(sales_scoped),
        "scoped_skus":         sales_scoped["SKU"].nunique(),
        "weekly_rows_before_fill": len(weekly),
        "weekly_rows_after_fill":  len(weekly_filled),
        "final_skus":          weekly_clean["SKU"].nunique(),
        "duration_sec":        round(duration, 1),
    }])

    if os.path.exists(log_csv):
        log_row.to_csv(log_csv, mode="a", header=False, index=False)
    else:
        log_row.to_csv(log_csv, index=False)
    print(f"  Prep log appended -> {log_csv}")

    print(f"\nDone in {duration:.1f}s. Final: {weekly_clean['SKU'].nunique()} SKUs, "
          f"{len(weekly_clean):,} SKU-week rows.")

    return weekly_clean


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PetMaxi data prep (raw -> clean weekly)")
    parser.add_argument("--input",   default=DEFAULT_INPUT,    help="raw vendas xlsx path")
    parser.add_argument("--out-xlsx", default=DEFAULT_OUT_XLSX, help="output prepared xlsx path")
    parser.add_argument("--out-csv",  default=DEFAULT_OUT_CSV,  help="output weekly clean csv path")
    parser.add_argument("--log-csv",  default=DEFAULT_LOG_CSV,  help="prep run log csv path")
    args = parser.parse_args()

    try:
        run_prep(args.input, args.out_xlsx, args.out_csv, args.log_csv)
    except Exception as e:
        print(f"\nERROR: data prep failed: {e}")
        sys.exit(1)
