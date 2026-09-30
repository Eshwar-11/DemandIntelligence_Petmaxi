# PetMaxi Demand Intelligence Dashboard - Deployment Notes

Flask backend reading SQLite, serving the HTML dashboard, with two live NETPET
feeds (open orders + inventory). No models run at request time; the 14-day batch
job builds the forecast DB separately.

## 1. Run it

```bash
pip install flask flask_cors pandas openpyxl requests truststore
python dashboard_backend_v7_ab.py
```

Serves at `http://localhost:5000/petmaxi-dashboard/` (or the LAN IP on port 5000).
The reloader runs the file twice by design; the startup NETPET pulls fire only in
the serving child, so you see the fetch logs once.

## 2. Environment variables

| Variable | Purpose | Example / default |
|---|---|---|
| `NETPET_BASE_URL` | NETPET host. Use the SAN-covered hostname, not an IP. | `https://netpet.petmaxi.local` |
| `NETPET_INSECURE` | `1` skips TLS verification. Nonprod demo on the trusted VPN ONLY. Never production. | unset |
| `NETPET_CA_BUNDLE` | Absolute path to the `petMaxi-CA` cert (PEM). The issuing CA, NOT the server leaf. | `C:\certs\petMaxi-CA.pem` |
| `NETPET_CA_CHAIN_ONLY` | `1` verifies the chain but skips hostname check. Only needed to reach the box by a SAN-omitted IP. Unnecessary with the hostname. | unset |
| `NETPET_NO_TRUSTSTORE` | `1` disables the OS trust store injection. | unset |
| `PETMAXI_DB_PATH` | Forecast SQLite DB (batch output). | `db/petmaxi_v7.db` |
| `PETMAXI_DATA_PATH` | Excel source for RM formulas / sack weights. | `data/vendas_1_1.xlsx` |
| `PETMAXI_ORDERS_DB_PATH` | Open-orders snapshot DB (auto-created). | `db/petmaxi_open_orders.db` |
| `PETMAXI_INVENTORY_DB_PATH` | Inventory snapshot DB (auto-created). | `db/petmaxi_inventory.db` |
| `PETMAXI_OO_GAP_BASIS` | Which order quantity folds into the gap: `pending` (default), `ordered`, `reserved`, `transformed`. | `pending` |
| `PETMAXI_GAP_PCT_BASE` | gap% denominator: `demand` (forecast+orders, default) or `forecast`. | `demand` |
| `PETMAXI_BASE_PATH` | URL mount path. | `/petmaxi-dashboard` |

Set the variable and launch in the SAME shell, or it will not reach the process:

```
set NETPET_BASE_URL=https://netpet.petmaxi.local && python dashboard_backend_v7_ab.py   (cmd)
$env:NETPET_BASE_URL="https://netpet.petmaxi.local"; python dashboard_backend_v7_ab.py   (PowerShell)
```

In an IDE, set them in the run configuration's environment section.

## 3. TLS setup (the one thing that trips people up)

The server cert (`CN=intranet.petmaxi`) is issued by `petMaxi-CA`. Its SAN covers
`*.petmaxi.local`, so connect by `https://netpet.petmaxi.local`, never by
`https://10.0.201.22` (that IP is not in the SAN and fails hostname checks).

To verify TLS you must trust the ISSUING CA (`petMaxi-CA`), not the server leaf.
The file named `netpet-root.pem` floating around is the LEAF, not the CA; using
it as `NETPET_CA_BUNDLE` gives "unable to get local issuer certificate".

Pick one:

- Domain-joined server (recommended): `petMaxi-CA` is already in the machine
  trust store via ADCS. `pip install truststore` and the app uses it
  automatically. No bundle, no insecure flag.
- Off-domain host: export `petMaxi-CA` to PEM and set `NETPET_CA_BUNDLE` to its
  absolute path. Confirm you have the CA, not a leaf:
  `openssl x509 -in petMaxi-CA.pem -noout -subject -issuer`
  Both lines should read `CN=petMaxi-CA` (self-signed root).
- Demo unblock only: `NETPET_INSECURE=1`.

## 4. Gap logic (what changed)

```
available   = FG stock in tonnes from NETPET /inventory/ (article types 3 + 4)
open_orders = chosen basis (default Pending) prorated to tonnes
gap         = forecast_total + open_orders - available
gap_pct     = gap / (forecast_total + open_orders) * 100     [demand basis]
urgency     = red >50%, yellow >threshold, green otherwise (1W threshold = 0)
```

The inventory feed is positive-stock-only, so a SKU missing from it genuinely has
0 tonnes available and its gap is still computable. If there is no live inventory
snapshot, the code falls back to the legacy `sku_inventory` table, then to
"uncovered".

## 5. Files

- `dashboard_backend_v7_ab.py` - Flask app, all API routes, gap + tier logic.
- `open_orders_store.py` - NETPET open-orders feed + the shared NETPET HTTP
  client (all TLS/CA handling lives here).
- `netpet_inventory_store.py` - NETPET inventory feed; reuses that client.
- `run_batch_v7.py` / `forecasting_engine_v7.py` - the fortnightly batch that
  builds `petmaxi_v7.db`. Not run by the web app.
- `templates/` - `landing_v7_ab.html`, `dashboard_v7_ab.html`, etc.

## 6. Health check after boot

Look for these in the log:

```
[open_orders] startup live fetch OK: {...}
[inventory] startup live fetch OK: {...}
```

Then hit `GET /petmaxi-dashboard/api/inventory_status` and
`GET /petmaxi-dashboard/api/open_orders` to confirm both snapshots loaded.
