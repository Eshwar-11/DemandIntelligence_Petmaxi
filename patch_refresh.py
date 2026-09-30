"""
patch_refresh.py - make the dashboard Refresh button pull inventory too.

The Refresh button (id=ooRefreshBtn -> refreshOpenOrders) currently only hits
/api/open_orders/refresh. This rewrites the success branch of that function so
the same click ALSO hits /api/inventory/refresh, reloads the WES/inventory
panel, and re-opens the current SKU detail so its stock and gap update together.

Usage:
    python patch_refresh.py                       # patches dashboard_v7_ab_api.html in place (.bak kept)
    python patch_refresh.py path/to/file.html     # patch a specific file

Safe to re-run: if the new code is already present it does nothing. It only
edits when the exact old block is found once, and writes a .bak first.
"""

import sys
import os
import shutil

OLD = """    const d = await r.json();
    if (d && d.ok) {
      await loadOpenOrders();
      showToast(`Open orders updated · ${d.n_orders} orders`, 3000);
    } else {
      showOoTip(d && d.last_success ? d.last_success.fetched_at : null);
    }"""

NEW = """    const d = await r.json();
    // Also refresh inventory in the same click so the SKU card's stock and the
    // production gap move together (both are live from NETPET). Best-effort:
    // an inventory hiccup never blocks the open-orders result we already have.
    let inv = null;
    try {
      const ir = await fetch(API + '/api/inventory/refresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: 'live' })
      });
      inv = await ir.json();
    } catch (e) { /* inventory refresh is best-effort */ }
    if (d && d.ok) {
      await loadOpenOrders();
      await loadExternalInventory();
      if (STATE.selectedSku) { STATE.ooCache = {}; loadSkuDetail(STATE.selectedSku); }
      const invMsg = (inv && inv.ok) ? ` · inventory ${inv.n_rows} rows` : '';
      showToast(`Open orders updated · ${d.n_orders} orders${invMsg}`, 3000);
    } else {
      showOoTip(d && d.last_success ? d.last_success.fetched_at : null);
    }"""

MARKER = "/api/inventory/refresh"


def patch(path: str) -> bool:
    if not os.path.exists(path):
        print(f"  skip (not found): {path}")
        return False
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    if MARKER in html:
        print(f"  already patched: {path}")
        return False

    n = html.count(OLD)
    if n == 0:
        print(f"  OLD block not found in {path} - refreshOpenOrders may have been "
              f"edited already. Nothing changed.")
        return False
    if n > 1:
        print(f"  OLD block appears {n} times in {path} - too ambiguous to patch "
              f"automatically. Nothing changed.")
        return False

    shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html.replace(OLD, NEW, 1))
    print(f"  patched: {path}  (backup at {path}.bak)")
    return True


if __name__ == "__main__":
    targets = sys.argv[1:] or ["templates/dashboard_v7_ab_api.html"]
    any_done = False
    for t in targets:
        any_done = patch(t) or any_done
    if not any_done:
        print("No file was changed.")
    else:
        print("Done. Restart the Flask app (or just hard-reload the page) to pick it up.")
