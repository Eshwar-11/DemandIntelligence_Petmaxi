"""
NETPET Inventory Store - live FG / RM stock feed
=================================================
Replaces the missing sku_inventory table. Inventory now comes from NETPET's
/fabrica/addverb/inventory/ endpoint (Inventory_Interface.md), stored in its
OWN sqlite file so the 14-day forecast batch never touches it, exactly like
open_orders_store. Each fetch REPLACES the snapshot.

Why this lets the dashboard finally compute an honest gap everywhere
--------------------------------------------------------------------
The interface returns POSITIVE STOCK ONLY (spec 7.4). So a SKU that is absent
from the feed genuinely has ZERO available stock - not "unknown". That means
`available_tons_for_sku` can return 0.0 for a missing SKU without guessing,
and the gap becomes computable for every forecastable SKU instead of only the
ones that happened to be in a stale inventory extract.

Article types (spec section 6): only 3,4,6,9 are in scope.
    3 Mercadoria        (goods / merchandise / snacks / silica)
    4 Produto Acabado   (finished product)
    6 Materia Prima     (raw material)
    9 Embal. de Consumo (packaging material)

FG availability (what the forecast gap nets against) = article types 3 + 4,
matching the original vendas scope (Produto Acabado + Mercadoria). RM stock is
type 6, packaging type 9 - exposed via stock_tons_all_skus(article_types=...)
for the RM view, kept out of the FG gap.

Inventory is at SKU + Batch level, so one SKU can appear on several rows; the
aggregates SUM WeightTON across batches per SKU.

TLS / base URL: reuses open_orders_store.netpet_get, so the CA / hostname /
verify handling lives in exactly one place (see that module's docstring).
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from open_orders_store import netpet_get   # shared NETPET HTTP client (TLS lives there)

_HERE = os.path.dirname(os.path.abspath(__file__))
INVENTORY_DB_PATH = os.environ.get(
    "PETMAXI_INVENTORY_DB_PATH", os.path.join(_HERE, "db", "petmaxi_inventory.db")
)
NETPET_INVENTORY_PATH = "/fabrica/addverb/inventory/"

# Article-type scope
RELEVANT_ARTICLE_TYPES = {3, 4, 6, 9}
FG_ARTICLE_TYPES = {3, 4}       # Mercadoria + Produto Acabado -> nets the FG gap
RM_ARTICLE_TYPES = {6}          # Materia Prima
PACKAGING_ARTICLE_TYPES = {9}   # Embal. de Consumo


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db():
    os.makedirs(os.path.dirname(INVENTORY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS inventory_batches (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            sku                       TEXT,
            description               TEXT,
            family                    TEXT,
            subfamily                 TEXT,
            batch                     TEXT,
            stock_quantity            REAL,
            weight_ton                REAL,
            production_date           TEXT,
            expiration_date           TEXT,
            shelf_life_days           INTEGER,
            remaining_shelf_life_days INTEGER,
            article_type_id           INTEGER,
            article_type              TEXT,
            model                     TEXT,
            fetched_at                TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_inv_sku  ON inventory_batches(sku);
        CREATE INDEX IF NOT EXISTS idx_inv_type ON inventory_batches(article_type_id);
        CREATE TABLE IF NOT EXISTS inventory_fetch_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT,
            source     TEXT,
            n_rows     INTEGER,
            status     TEXT,
            message    TEXT
        );
    """)
    conn.commit()
    conn.close()


def _to_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def ingest_response(resp_json: dict, source: str = "live") -> dict:
    """Replaces the current inventory snapshot with resp_json['data'], keeping
    only in-scope article types (3,4,6,9). Raises ValueError on non-success
    meta; the previous snapshot survives (never a partial write)."""
    meta = resp_json.get("meta", {})
    if meta.get("status") != "Success":
        raise ValueError(f"NETPET inventory returned non-success meta: {meta}")

    rows = resp_json.get("data") or []
    fetched_at = _now_iso()
    n_rows = 0

    init_db()
    conn = _db()
    try:
        conn.execute("DELETE FROM inventory_batches")
        for r in rows:
            atid = _to_int(r.get("ArticleTypeId"))
            if atid not in RELEVANT_ARTICLE_TYPES:
                continue   # spec 11.4: other article types are out of scope
            n_rows += 1
            conn.execute(
                """INSERT INTO inventory_batches
                   (sku, description, family, subfamily, batch, stock_quantity,
                    weight_ton, production_date, expiration_date, shelf_life_days,
                    remaining_shelf_life_days, article_type_id, article_type,
                    model, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(r["Sku"]) if r.get("Sku") is not None else None,
                    r.get("Description"), r.get("Family"), r.get("SubFamily"),
                    str(r["Batch"]) if r.get("Batch") is not None else None,
                    r.get("StockQuantity"), r.get("WeightTON"),
                    r.get("ProductionDate"), r.get("ExpirationDate"),
                    _to_int(r.get("ShelfLifeDays")),
                    _to_int(r.get("RemainingShelfLifeDays")),
                    atid, r.get("ArticleType"), r.get("Model"), fetched_at,
                ),
            )
        conn.execute(
            """INSERT INTO inventory_fetch_log
               (fetched_at, source, n_rows, status, message)
               VALUES (?,?,?,?,?)""",
            (fetched_at, source, n_rows, "Success", meta.get("messageDescription")),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"fetched_at": fetched_at, "n_rows": n_rows}


def log_failure(source: str, message: str):
    init_db()
    conn = _db()
    conn.execute(
        """INSERT INTO inventory_fetch_log (fetched_at, source, n_rows, status, message)
           VALUES (?,?,?,?,?)""",
        (_now_iso(), source, 0, "Error", message),
    )
    conn.commit()
    conn.close()


def fetch_and_store(headers: Optional[dict] = None) -> dict:
    """Hits the live NETPET inventory endpoint via the shared client. On
    failure logs an Error row and re-raises; the previous snapshot stays."""
    try:
        resp_json = netpet_get(NETPET_INVENTORY_PATH, headers=headers)
        return ingest_response(resp_json, source="live")
    except Exception as e:
        log_failure("live", str(e))
        raise


def load_fixture(path: str) -> dict:
    import json
    with open(path, "r", encoding="utf-8") as f:
        resp_json = json.load(f)
    return ingest_response(resp_json, source="fixture")


# ── Read helpers ────────────────────────────────────────────────────────────

def has_inventory() -> bool:
    """True if a snapshot is currently loaded (any in-scope row)."""
    init_db()
    conn = _db()
    row = conn.execute("SELECT 1 FROM inventory_batches LIMIT 1").fetchone()
    conn.close()
    return row is not None


def get_last_fetch() -> Optional[dict]:
    init_db()
    conn = _db()
    row = conn.execute(
        "SELECT * FROM inventory_fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_last_success() -> Optional[dict]:
    init_db()
    conn = _db()
    row = conn.execute(
        "SELECT * FROM inventory_fetch_log WHERE status = 'Success' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _stock_tons_map(article_types: Iterable[int]) -> dict:
    types = list(article_types)
    if not types:
        return {}
    placeholders = ",".join("?" for _ in types)
    init_db()
    conn = _db()
    rows = conn.execute(
        f"""SELECT sku,
                   COALESCE(SUM(weight_ton), 0)     AS on_hand_tons,
                   COALESCE(SUM(stock_quantity), 0) AS stock_qty,
                   COUNT(DISTINCT batch)            AS n_batches
            FROM inventory_batches
            WHERE sku IS NOT NULL AND article_type_id IN ({placeholders})
            GROUP BY sku""",
        types,
    ).fetchall()
    conn.close()
    return {
        str(r["sku"]): {
            "on_hand_tons": round(r["on_hand_tons"], 3),
            "stock_qty":    round(r["stock_qty"], 2),
            "n_batches":    int(r["n_batches"]),
        }
        for r in rows
    }


def available_all_skus() -> dict:
    """{sku: on_hand_tons} for FG article types (3,4). This is the `available`
    the forecast gap nets against. Positive-stock-only feed => a SKU missing
    from this map has 0.0 available, not unknown."""
    return {sku: v["on_hand_tons"] for sku, v in _stock_tons_map(FG_ARTICLE_TYPES).items()}


def available_tons_for_sku(sku: str) -> float:
    return float(available_all_skus().get(str(sku), 0.0))


def fg_detail_all_skus() -> dict:
    """Richer FG map (tons + sales-unit qty + batch count) for the SKU detail
    inventory card."""
    return _stock_tons_map(FG_ARTICLE_TYPES)


def rm_stock_all_skus() -> dict:
    """{rm_sku: on_hand_tons} for raw materials (type 6), for the RM view."""
    return {sku: v["on_hand_tons"] for sku, v in _stock_tons_map(RM_ARTICLE_TYPES).items()}


def get_batches_for_sku(sku: str) -> list:
    """All batch rows for one SKU (any in-scope type), newest expiry first, for
    a batch/shelf-life drill-down."""
    init_db()
    conn = _db()
    rows = conn.execute(
        """SELECT * FROM inventory_batches WHERE sku = ?
           ORDER BY remaining_shelf_life_days IS NULL, remaining_shelf_life_days ASC""",
        (str(sku),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
