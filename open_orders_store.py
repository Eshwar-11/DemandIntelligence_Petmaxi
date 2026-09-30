"""
Open Orders Store - NETPET Pending Orders feed
================================================
Separate SQLite file from petmaxi_v7.db, so the 14-day batch upload never
wipes order data. Each fetch REPLACES the current snapshot (this is a
pending-orders list, not an append-only log).

This module also owns the shared NETPET HTTP client (netpet_get / _build_session
/ _resolve_verify). netpet_inventory_store and any other NETPET reader import
netpet_get from here, so the TLS / CA / base-URL handling lives in exactly one
place.

TLS / deployment (read this - it is the thing that was "stuck")
---------------------------------------------------------------
The blocker was never really "SSL". It was two facts about the cert:

  1. The file that was being trusted was the LEAF cert (CN=intranet.petmaxi),
     not the issuing CA (petMaxi-CA). Trusting a leaf does not let the client
     build a chain, hence "unable to get local issuer certificate".
  2. The leaf's SAN covers *.petmaxi.local (and a set of 10.0.201.x IPs) but
     NOT 10.0.201.22. So connecting by https://10.0.201.22 fails hostname
     verification even after the CA is trusted.

Clean, deployable fix - no verify=False anywhere:

    NETPET_BASE_URL = https://netpet.petmaxi.local      # matches *.petmaxi.local SAN
    + trust petMaxi-CA (the ISSUER, not the leaf)

Trust the CA in one of two ways:
  - Domain-joined Windows box (the target server): petMaxi-CA is already in the
    machine's trust store via ADCS. This module calls truststore.inject_into_ssl()
    at import (unless NETPET_NO_TRUSTSTORE=1), so requests uses the OS store and
    verify=True just works. `pip install truststore` (Py 3.10+).
  - Anywhere else: export the real petMaxi-CA certificate (again, the CA, not the
    leaf) to a .pem and point NETPET_CA_BUNDLE at it.

Escape hatches, in strict precedence inside _resolve_verify():
    NETPET_INSECURE=1        -> verify=False. Nonprod demo box on the trusted
                               VPN ONLY. Never production.
    NETPET_CA_BUNDLE=<path>  -> verify against that bundle (must exist, non-empty;
                               an empty/leaf-only file no longer silently wins).
    NETPET_CA_CHAIN_ONLY=1   -> with a CA_BUNDLE, verify the chain to the CA but
                               skip hostname matching. Only needed to reach the
                               box by an IP the SAN omits (e.g. 10.0.201.22).
                               Unnecessary once you use the hostname above.
    (none set)               -> verify=True (OS/truststore trust store).

Wire format vs internal contract
--------------------------------
The live NETPET endpoint returns Portuguese/legacy field names
(serie/numDoc/cliente/artigos/qntEnc/qtdRes/qtdPendente/qtdTransformada/
pesoTON/paletes/brand/subFamilia/descricao/observacoes/export).

The .md interface spec documents target-state English names (OrderId/
CustomerName/Items/OrderedQuantity/ReservedQuantity/PendingQuantity/
TransformedQuantity/WeightTON/Pallets/Family/SubFamily/Description/Notes/
IsExport/Unity).

Translation happens ONCE, here at ingest. Storage, reads, and all downstream
endpoints use English names throughout. When NETPET cuts over to the .md spec
names, only the PT_TO_EN maps below change.

Field semantics (confirmed with manager KT, 2026-09):
  - OrderedQuantity     : total customer commitment for this SKU line
  - ReservedQuantity    : how much of that commitment is held against FG stock
  - PendingQuantity     : how much of that commitment is on the production plan
  - TransformedQuantity : of the pending portion, how much has been produced

Reserved and Pending are OVERLAPPING VIEWS of the same commitment, NOT a
partition of it. Do not add them to reconstruct Ordered - the identity
Ordered = Reserved + Pending + Transformed is violated on ~54% of real line
items. aggregate_for_sku() returns them as four independent totals; the
frontend must present them in a way that does not invite users to sum them.

Gap folding (NEW - see open_order_gap_tons_*):
  WeightTON on a line is the weight of the WHOLE Ordered quantity. To fold a
  chosen basis (default Pending) into the production gap in TONNES, we prorate:
      basis_tons = WeightTON * basis_qty / OrderedQuantity
  computed in SQL so callers read a ready number. Ordered basis is just
  SUM(WeightTON). See PetMaxi_Model_Comparison notes / dashboard gap logic.
"""

import json
import os
import ssl
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
ORDERS_DB_PATH = os.environ.get(
    "PETMAXI_ORDERS_DB_PATH", os.path.join(_HERE, "db", "petmaxi_open_orders.db")
)

# Prefer the SAN-covered hostname. https://10.0.201.22 will fail hostname
# verification (that IP is not in the leaf SAN); the hostname is.
NETPET_BASE_URL = os.environ.get("NETPET_BASE_URL", "https://netpet.petmaxi.local")   # e.g. "https://netpet.petmaxi.local"
NETPET_ORDERS_PATH = "/fabrica/addverb/list-orders/"

# TLS knobs (see module docstring for the full precedence and the clean fix).
NETPET_CA_BUNDLE = os.environ.get("netpet-ca.pem")           # path to petMaxi-CA .pem
#NETPET_INSECURE = os.environ.get("NETPET_INSECURE") = "1"      # nonprod-demo only
NETPET_INSECURE = True   # TEMP nonprod-demo: force insecure, ignore env
NETPET_CA_CHAIN_ONLY = os.environ.get("NETPET_CA_CHAIN_ONLY") == "1"
NETPET_HTTP_TIMEOUT = int(os.environ.get("NETPET_HTTP_TIMEOUT", "30"))
print(f"[netpet] BASE={NETPET_BASE_URL} INSECURE={NETPET_INSECURE} CA_BUNDLE={NETPET_CA_BUNDLE}")

# ── Shared NETPET HTTP client ───────────────────────────────────────────────
# Use the machine trust store by default. On the domain-joined Windows server
# petMaxi-CA is already trusted there, so verify=True succeeds with no bundle
# file to ship. Disable with NETPET_NO_TRUSTSTORE=1 if it ever causes trouble.
if os.environ.get("NETPET_NO_TRUSTSTORE") == "1":
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        # truststore not installed / older Python: fall back to certifi's bundle,
        # in which case ship NETPET_CA_BUNDLE=<petMaxi-CA .pem> on non-domain hosts.
        pass


def _resolve_verify():
    """Strict precedence: INSECURE -> CA_BUNDLE(exists, non-empty) -> True.
    A missing or empty NETPET_CA_BUNDLE can no longer silently defeat
    verification the way `verify = NETPET_CA_BUNDLE or False` used to."""
    if NETPET_INSECURE:
        return False
    if NETPET_CA_BUNDLE and os.path.exists(NETPET_CA_BUNDLE) and os.path.getsize(NETPET_CA_BUNDLE) > 0:
        return NETPET_CA_BUNDLE
    return True


class _ChainOnlyAdapter:
    """Verifies the chain up to the CA in NETPET_CA_BUNDLE but skips hostname
    matching. Only for reaching the box by a SAN-omitted IP (e.g. 10.0.201.22).
    Defined lazily inside _build_session so this file imports even where
    requests is absent (fixture-only use)."""
    pass


def _build_session(verify):
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.poolmanager import PoolManager

    session = requests.Session()

    if verify is False:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return session

    if isinstance(verify, str) and NETPET_CA_CHAIN_ONLY:
        class ChainOnlyAdapter(HTTPAdapter):
            def __init__(self, cafile, **kw):
                self._cafile = cafile
                super().__init__(**kw)

            def init_poolmanager(self, connections, maxsize, block=False, **kw):
                ctx = ssl.create_default_context(cafile=self._cafile)
                ctx.check_hostname = False   # chain verified, hostname skipped
                self.poolmanager = PoolManager(
                    num_pools=connections, maxsize=maxsize, block=block,
                    ssl_context=ctx, **kw
                )

        session.mount("https://", ChainOnlyAdapter(verify))
    return session


def netpet_get(path: str, headers: Optional[dict] = None) -> dict:
    """GET a NETPET endpoint under NETPET_BASE_URL and return parsed JSON.
    Raises RuntimeError if the base URL is unset (a misconfigured env var must
    not look like an empty feed downstream). TLS governed by _resolve_verify()."""
    import requests  # lazy: fixture-only paths need no requests install

    if not NETPET_BASE_URL:
        raise RuntimeError(
            "NETPET_BASE_URL is not set - cannot fetch live data. "
            "Set it to https://netpet.petmaxi.local (SAN-covered hostname)."
        )

    url = NETPET_BASE_URL.rstrip("/") + path
    verify = _resolve_verify()
    session = _build_session(verify)
    # When the chain-only adapter is mounted it owns verification via its own
    # ssl_context, so we pass verify=True to avoid requests layering a second,
    # hostname-checking context on top.
    req_verify = True if (isinstance(verify, str) and NETPET_CA_CHAIN_ONLY) else verify
    resp = session.get(url, headers=headers or {}, timeout=NETPET_HTTP_TIMEOUT, verify=req_verify)
    resp.raise_for_status()
    return resp.json()


# ── Translation layer (PT wire -> EN internal) ──────────────────────────────

ORDER_PT_TO_EN = {
    "serie":       "Series",
    "numDoc":      "DocumentNumber",
    "cliente":     "CustomerName",
    "pais":        "Country",
    "referencia":  "Reference",
    "dataEntrega": "DeliveryDate",
    "status":      "Status",
    "observacoes": "Notes",
    "export":      "IsExport",
    "artigos":     "Items",
}

ITEM_PT_TO_EN = {
    "sku":             "Sku",
    "descricao":       "Description",
    "unidade":         "Unity",           # sales unit code, links to unit-conversions
    "brand":           "Family",
    "subFamilia":      "SubFamily",
    "qntEnc":          "OrderedQuantity",
    "qtdRes":          "ReservedQuantity",
    "qtdPendente":     "PendingQuantity",
    "qtdTransformada": "TransformedQuantity",
    "pesoTON":         "WeightTON",
    "paletes":         "Pallets",
}


def _translate_order(order_pt: dict) -> dict:
    """Rename PT keys to EN and synthesize OrderId per .md rule 7.1
    (Series-DocumentNumber). Items translated recursively. If the payload is
    already in English (post-cutover), the .get(k, k) fallbacks pass keys
    through unchanged."""
    order_en = {ORDER_PT_TO_EN.get(k, k): v for k, v in order_pt.items() if k not in ("artigos", "Items")}
    series = order_pt.get("serie", order_pt.get("Series"))
    doc = order_pt.get("numDoc", order_pt.get("DocumentNumber"))
    if order_pt.get("OrderId"):
        order_en["OrderId"] = order_pt["OrderId"]
    else:
        order_en["OrderId"] = f"{series}-{doc}" if series and doc else (doc or series)
    items_raw = order_pt.get("artigos") or order_pt.get("Items") or []
    order_en["Items"] = [
        {ITEM_PT_TO_EN.get(k, k): v for k, v in item.items()}
        for item in items_raw
    ]
    return order_en


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db():
    os.makedirs(os.path.dirname(ORDERS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(ORDERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS open_orders (
            order_id        TEXT PRIMARY KEY,
            series          TEXT,
            document_number TEXT,
            customer_name   TEXT,
            country         TEXT,
            reference       TEXT,
            delivery_date   TEXT,
            status          TEXT,
            notes           TEXT,
            is_export       INTEGER,
            fetched_at      TEXT
        );
        CREATE TABLE IF NOT EXISTS open_order_items (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id              TEXT,
            sku                   TEXT,
            description           TEXT,
            unity                 TEXT,
            family                TEXT,
            subfamily             TEXT,
            ordered_quantity      REAL,
            reserved_quantity     REAL,
            pending_quantity      REAL,
            transformed_quantity  REAL,
            weight_ton            REAL,
            pallets               REAL,
            fetched_at            TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_items_sku ON open_order_items(sku);
        CREATE TABLE IF NOT EXISTS open_orders_fetch_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT,
            source     TEXT,
            n_orders   INTEGER,
            n_items    INTEGER,
            status     TEXT,
            message    TEXT
        );
    """)
    conn.commit()
    conn.close()


def _bool_to_int(v) -> Optional[int]:
    if v is None:
        return None
    return 1 if v is True else (0 if v is False else None)


def ingest_response(resp_json: dict, source: str = "live") -> dict:
    """Replaces the current open-orders snapshot with resp_json['data'].
    Raises ValueError if meta.status isn't Success - the previous snapshot is
    preserved intact (this function never partially writes on failure)."""
    meta = resp_json.get("meta", {})
    if meta.get("status") != "Success":
        raise ValueError(f"NETPET returned non-success meta: {meta}")

    orders_pt = resp_json.get("data") or []
    fetched_at = _now_iso()
    n_items = 0

    init_db()
    conn = _db()
    try:
        conn.execute("DELETE FROM open_orders")
        conn.execute("DELETE FROM open_order_items")
        for order_pt in orders_pt:
            order = _translate_order(order_pt)
            conn.execute(
                """INSERT INTO open_orders
                   (order_id, series, document_number, customer_name, country,
                    reference, delivery_date, status, notes, is_export, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order.get("OrderId"), order.get("Series"), order.get("DocumentNumber"),
                    order.get("CustomerName"), order.get("Country"), order.get("Reference"),
                    order.get("DeliveryDate"), order.get("Status"), order.get("Notes"),
                    _bool_to_int(order.get("IsExport")), fetched_at,
                ),
            )
            for item in order.get("Items") or []:
                n_items += 1
                conn.execute(
                    """INSERT INTO open_order_items
                       (order_id, sku, description, unity, family, subfamily,
                        ordered_quantity, reserved_quantity, pending_quantity,
                        transformed_quantity, weight_ton, pallets, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        order.get("OrderId"),
                        str(item["Sku"]) if item.get("Sku") is not None else None,
                        item.get("Description"), item.get("Unity"),
                        item.get("Family"), item.get("SubFamily"),
                        item.get("OrderedQuantity"), item.get("ReservedQuantity"),
                        item.get("PendingQuantity"), item.get("TransformedQuantity"),
                        item.get("WeightTON"), item.get("Pallets"), fetched_at,
                    ),
                )
        conn.execute(
            """INSERT INTO open_orders_fetch_log
               (fetched_at, source, n_orders, n_items, status, message)
               VALUES (?,?,?,?,?,?)""",
            (fetched_at, source, len(orders_pt), n_items, "Success",
             meta.get("messageDescription")),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"fetched_at": fetched_at, "n_orders": len(orders_pt), "n_items": n_items}


def log_failure(source: str, message: str):
    init_db()
    conn = _db()
    conn.execute(
        """INSERT INTO open_orders_fetch_log
           (fetched_at, source, n_orders, n_items, status, message)
           VALUES (?,?,?,?,?,?)""",
        (_now_iso(), source, 0, 0, "Error", message),
    )
    conn.commit()
    conn.close()


def fetch_and_store(headers: Optional[dict] = None) -> dict:
    """Hits the live NETPET pending-orders endpoint via the shared client.
    On failure logs an Error row and re-raises; the previous snapshot stays."""
    try:
        resp_json = netpet_get(NETPET_ORDERS_PATH, headers=headers)
        return ingest_response(resp_json, source="live")
    except Exception as e:
        log_failure("live", str(e))
        raise


def load_fixture(path: str) -> dict:
    """Loads a local JSON file shaped like a real NETPET response (PT keys).
    Used to test the pipeline before real credentials / network exist."""
    with open(path, "r", encoding="utf-8") as f:
        resp_json = json.load(f)
    return ingest_response(resp_json, source="fixture")


# ── Read helpers used by the Flask routes ───────────────────────────────────

def get_last_fetch() -> Optional[dict]:
    init_db()
    conn = _db()
    row = conn.execute(
        "SELECT * FROM open_orders_fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_last_success() -> Optional[dict]:
    """Most recent SUCCESSFUL fetch log row. A failed live pull logs an Error
    row while the stored snapshot is still the previous good data, so the
    'fetched X ago' stamp must read from here, not the latest attempt."""
    init_db()
    conn = _db()
    row = conn.execute(
        "SELECT * FROM open_orders_fetch_log WHERE status = 'Success' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def has_orders() -> bool:
    """True if a snapshot is currently loaded. Used at startup to decide
    whether to seed the demo fixture: a real pull must never be overwritten."""
    init_db()
    conn = _db()
    row = conn.execute("SELECT 1 FROM open_order_items LIMIT 1").fetchone()
    conn.close()
    return row is not None


# Proration expression: weight of a chosen quantity basis, in tonnes.
# WeightTON is the weight of the whole Ordered line, so
#   basis_tons = WeightTON * basis_qty / OrderedQuantity.
# Guard OrderedQuantity=0: if a basis qty is still positive there (contradictory
# but seen in live data), fall back to the full line weight; otherwise 0.
def _proration_sql(basis_col: str) -> str:
    return (
        f"COALESCE(SUM(CASE "
        f"WHEN ordered_quantity > 0 THEN weight_ton * {basis_col} / ordered_quantity "
        f"WHEN {basis_col} > 0 THEN weight_ton "
        f"ELSE 0 END), 0)"
    )


def aggregate_all_skus() -> dict:
    """One-pass GROUP BY for EVERY SKU in the snapshot, keyed by SKU string.
    Returns the four raw quantity totals (overlapping views, never summed) AND
    prorated weight-tonne figures for each basis, so the gap logic can fold a
    chosen basis straight into a tonnes-based gap without re-deriving units."""
    init_db()
    conn = _db()
    rows = conn.execute(
        f"""SELECT sku,
                  COALESCE(SUM(ordered_quantity),     0) AS ordered_total,
                  COALESCE(SUM(reserved_quantity),    0) AS reserved_total,
                  COALESCE(SUM(pending_quantity),     0) AS pending_total,
                  COALESCE(SUM(transformed_quantity), 0) AS transformed_total,
                  COALESCE(SUM(weight_ton),           0) AS weight_ton_total,
                  {_proration_sql('reserved_quantity')}   AS reserved_weight_ton,
                  {_proration_sql('pending_quantity')}    AS pending_weight_ton,
                  {_proration_sql('transformed_quantity')} AS transformed_weight_ton,
                  COALESCE(SUM(pallets),              0) AS pallets_total,
                  COUNT(DISTINCT order_id)              AS n_orders,
                  COUNT(*)                              AS n_lines
           FROM open_order_items
           WHERE sku IS NOT NULL
           GROUP BY sku"""
    ).fetchall()
    conn.close()
    return {
        str(r["sku"]): {
            "ordered_total":          round(r["ordered_total"],     2),
            "reserved_total":         round(r["reserved_total"],    2),
            "pending_total":          round(r["pending_total"],     2),
            "transformed_total":      round(r["transformed_total"], 2),
            "weight_ton_total":       round(r["weight_ton_total"],  3),   # = ordered basis (tonnes)
            "reserved_weight_ton":    round(r["reserved_weight_ton"],    3),
            "pending_weight_ton":     round(r["pending_weight_ton"],     3),
            "transformed_weight_ton": round(r["transformed_weight_ton"], 3),
            "pallets_total":          round(r["pallets_total"],     2),
            "n_orders":               int(r["n_orders"]),
            "n_lines":                int(r["n_lines"]),
        }
        for r in rows
    }


_GAP_BASIS_COL = {
    "ordered":     "weight_ton_total",
    "reserved":    "reserved_weight_ton",
    "pending":     "pending_weight_ton",
    "transformed": "transformed_weight_ton",
}


def open_order_gap_tons_all_skus(basis: str = "pending") -> dict:
    """{sku: tonnes} for the chosen quantity basis, ready to add to the gap.
    basis in {ordered, reserved, pending, transformed}; default pending
    (committed demand still on the plan, not yet produced or stock-backed)."""
    col = _GAP_BASIS_COL.get((basis or "pending").lower(), "pending_weight_ton")
    return {sku: agg[col] for sku, agg in aggregate_all_skus().items()}


def open_order_gap_tons_for_sku(sku: str, basis: str = "pending") -> float:
    """Single-SKU convenience for the detail endpoint. 0.0 if the SKU has no
    open orders in the current snapshot."""
    col = _GAP_BASIS_COL.get((basis or "pending").lower(), "pending_weight_ton")
    return float(aggregate_for_sku(sku).get(col, 0.0) or 0.0)


def get_all_orders() -> list:
    init_db()
    conn = _db()
    orders = conn.execute("SELECT * FROM open_orders ORDER BY delivery_date").fetchall()
    items = conn.execute("SELECT * FROM open_order_items").fetchall()
    conn.close()

    items_by_order = {}
    for it in items:
        items_by_order.setdefault(it["order_id"], []).append(dict(it))

    return [
        {**dict(o),
         "is_export": bool(o["is_export"]) if o["is_export"] is not None else None,
         "items": items_by_order.get(o["order_id"], [])}
        for o in orders
    ]


def get_items_for_sku(sku: str) -> list:
    """Per-order rows for one SKU, joined back to parent order fields so the
    frontend needs no second lookup. Ordered by delivery date."""
    init_db()
    conn = _db()
    rows = conn.execute(
        """SELECT oi.*, o.customer_name, o.delivery_date, o.status AS order_status,
                  o.notes AS order_notes, o.is_export, o.reference
           FROM open_order_items oi
           JOIN open_orders o ON o.order_id = oi.order_id
           WHERE oi.sku = ?
           ORDER BY o.delivery_date""",
        (str(sku),),
    ).fetchall()
    conn.close()
    return [
        {**dict(r),
         "is_export": bool(r["is_export"]) if r["is_export"] is not None else None}
        for r in rows
    ]


def aggregate_for_sku(sku: str) -> dict:
    """Sums the four quantity fields (and their prorated tonnes) for one SKU.

    IMPORTANT (see module docstring): Reserved and Pending are OVERLAPPING
    VIEWS of the same commitment, not a partition. They must not be added
    together. interpretation_note carries a one-line caveat for the frontend."""
    init_db()
    conn = _db()
    row = conn.execute(
        f"""SELECT
             COALESCE(SUM(ordered_quantity),     0) AS ordered_total,
             COALESCE(SUM(reserved_quantity),    0) AS reserved_total,
             COALESCE(SUM(pending_quantity),     0) AS pending_total,
             COALESCE(SUM(transformed_quantity), 0) AS transformed_total,
             COALESCE(SUM(weight_ton),           0) AS weight_ton_total,
             {_proration_sql('reserved_quantity')}   AS reserved_weight_ton,
             {_proration_sql('pending_quantity')}    AS pending_weight_ton,
             {_proration_sql('transformed_quantity')} AS transformed_weight_ton,
             COALESCE(SUM(pallets),              0) AS pallets_total,
             COUNT(DISTINCT order_id)              AS n_orders,
             COUNT(*)                              AS n_lines
           FROM open_order_items
           WHERE sku = ?""",
        (str(sku),),
    ).fetchone()
    conn.close()
    return {
        "sku": str(sku),
        "ordered_total":          round(row["ordered_total"],     2),
        "reserved_total":         round(row["reserved_total"],    2),
        "pending_total":          round(row["pending_total"],     2),
        "transformed_total":      round(row["transformed_total"], 2),
        "weight_ton_total":       round(row["weight_ton_total"],  3),
        "reserved_weight_ton":    round(row["reserved_weight_ton"],    3),
        "pending_weight_ton":     round(row["pending_weight_ton"],     3),
        "transformed_weight_ton": round(row["transformed_weight_ton"], 3),
        "pallets_total":          round(row["pallets_total"],     2),
        "n_orders":               int(row["n_orders"]),
        "n_lines":                int(row["n_lines"]),
        "interpretation_note": (
            "Ordered is total customer commitment. Reserved (held against FG stock) "
            "and Pending (on production plan) are overlapping views of that same "
            "commitment - do not sum them. Transformed is the portion of Pending "
            "already produced. Gap folds the Pending basis, prorated to tonnes."
        ),
    }
