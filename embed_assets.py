"""
embed_assets.py
================
Makes all project HTML files self-contained for airgapped deployment.
Embeds Google Fonts (Manrope + JetBrains Mono) as base64 woff2 and
Chart.js as inline <script>, so nothing needs to be fetched at runtime.

Run this ONCE locally before packaging the .tar:

    python embed_assets.py --static-dir static --templates-dir templates

It reads font files from static/fonts/ and chart.umd.min.js from
static/, then patches each HTML file IN PLACE (makes a .bak backup
first). Safe to rerun — it detects already-embedded files and skips.

If you don't have the font files yet, the script downloads them for you
(needs network access on this one-time run, never again after).
"""

import argparse
import base64
import os
import re
import shutil
import urllib.request


MANROPE_WEIGHTS = [400, 500, 600, 700, 800]
JETBRAINS_WEIGHTS = [400, 500, 600]

GOOGLE_FONTS_CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Manrope:wght@{manrope}&"
    "family=JetBrains+Mono:wght@{jetbrains}&display=swap"
).format(
    manrope=";".join(str(w) for w in MANROPE_WEIGHTS),
    jetbrains=";".join(str(w) for w in JETBRAINS_WEIGHTS),
)

CHART_JS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
CHART_JS_FALLBACK = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"


def download_file(url, dest):
    print(f"    Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def ensure_chart_js(static_dir):
    path = os.path.join(static_dir, "chart.umd.min.js")
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        print(f"  chart.umd.min.js found ({os.path.getsize(path):,} bytes)")
        return path
    print("  chart.umd.min.js missing or too small, downloading...")
    try:
        download_file(CHART_JS_CDN, path)
    except Exception:
        download_file(CHART_JS_FALLBACK, path)
    print(f"  Downloaded ({os.path.getsize(path):,} bytes)")
    return path


def ensure_google_fonts(fonts_dir):
    """Download Google Fonts woff2 files if not already present.
    Returns a CSS string with embedded base64 @font-face rules."""

    os.makedirs(fonts_dir, exist_ok=True)

    # Step 1: get the CSS from Google (contains URLs to actual woff2 files)
    css_path = os.path.join(fonts_dir, "_google_fonts.css")
    if not os.path.exists(css_path):
        print("  Downloading Google Fonts CSS...")
        download_file(GOOGLE_FONTS_CSS_URL, css_path)

    css = open(css_path).read()

    # Step 2: find all woff2 URLs, download each, replace with base64
    woff2_urls = re.findall(r'url\((https://fonts\.gstatic\.com/[^)]+\.woff2)\)', css)
    for url in woff2_urls:
        fname = url.split("/")[-1]
        local = os.path.join(fonts_dir, fname)
        if not os.path.exists(local):
            download_file(url, local)
        b64 = base64.b64encode(open(local, "rb").read()).decode()
        css = css.replace(url, f"data:font/woff2;base64,{b64}")

    print(f"  {len(woff2_urls)} font files embedded as base64")
    return css


def build_font_style_block(font_css):
    return f"<style>/* Embedded Google Fonts (Manrope + JetBrains Mono) — base64 woff2 */\n{font_css}\n</style>"


def embed_in_file(filepath, font_style_block, chart_js_code):
    """Patch one HTML file in place. Makes a .bak backup first."""

    if not os.path.exists(filepath):
        print(f"  SKIP {filepath} (not found)")
        return

    html = open(filepath, encoding="utf-8").read()

    if "/* Embedded Google Fonts" in html:
        print(f"  SKIP {filepath} (already embedded)")
        return

    # Backup
    shutil.copy2(filepath, filepath + ".bak")

    # ── Remove external font references ──
    # Google Fonts <link> tags
    html = re.sub(
        r'<link[^>]*href="https://fonts\.googleapis\.com[^"]*"[^>]*/?>',
        '', html)
    html = re.sub(
        r'<link[^>]*href="https://fonts\.gstatic\.com[^"]*"[^>]*/?>',
        '', html)
    # @import url(...fonts...)
    html = re.sub(
        r"@import\s+url\(['\"]?[^)]*fonts[^)]*['\"]?\)\s*;?",
        '', html)
    # Static font CSS links ({{ base_path }}/static/fonts/...)
    html = re.sub(
        r'<link[^>]*href="[^"]*static/fonts/[^"]*"[^>]*/?>',
        '', html)

    # ── Remove external Chart.js references ──
    html = re.sub(
        r'<script[^>]*src="https://cdn[^"]*chart[^"]*"[^>]*>\s*</script>',
        '', html, flags=re.IGNORECASE)
    html = re.sub(
        r'<script[^>]*src="[^"]*static/chart\.umd\.min\.js"[^>]*>\s*</script>',
        '', html)
    # Remove the CDN fallback inline check too
    html = re.sub(
        r"<script>if\s*\(typeof Chart === 'undefined'\).*?</script>",
        '', html)

    # ── Inject embedded assets right after <head> ──
    inject = f"\n{font_style_block}\n"
    if chart_js_code and ("Chart" in html or "chart" in html.lower()):
        inject += f"\n<script>/* Chart.js 4.4.1 embedded */\n{chart_js_code}\n</script>\n"

    html = html.replace("<head>", f"<head>{inject}", 1)

    open(filepath, "w", encoding="utf-8").write(html)
    print(f"  DONE {filepath}")


def main():
    parser = argparse.ArgumentParser(description="Embed fonts + Chart.js into all project HTML files")
    parser.add_argument("--static-dir", default="static",
                        help="path to static/ folder (contains chart.umd.min.js and fonts/)")
    parser.add_argument("--templates-dir", default="templates",
                        help="path to templates/ folder (contains all .html files)")
    args = parser.parse_args()

    print("[1] Ensuring Chart.js is available locally...")
    chart_path = ensure_chart_js(args.static_dir)
    chart_js = open(chart_path, encoding="utf-8").read()

    fonts_dir = os.path.join(args.static_dir, "fonts")
    print("[2] Ensuring Google Fonts are available locally...")
    font_css = ensure_google_fonts(fonts_dir)
    font_block = build_font_style_block(font_css)

    print("[3] Embedding into HTML files...")
    html_files = [
        os.path.join(args.templates_dir, "dashboard_v7.html"),
        os.path.join(args.templates_dir, "landing_v7.html"),
        os.path.join(args.templates_dir, "Customer_Intelligence.html"),
        os.path.join(args.templates_dir, "Metric_Analytics_1.html"),
    ]
    for f in html_files:
        embed_in_file(f, font_block, chart_js)

    print("\nAll files are now self-contained. No network access needed at runtime.")
    print("Originals backed up as .bak files.")


if __name__ == "__main__":
    main()
