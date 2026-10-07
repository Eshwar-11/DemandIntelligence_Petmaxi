# PetMaxi Demand Intelligence Dashboard — v8 (gap-aligned)
# -------------------------------------------------------------
# Minimal production image for the Flask backend + static assets.
# Build:   docker build -t petmaxi-dashboard:v8 .
# Run:     docker run -d --name petmaxi -p 5000:5000 \
#              -v $(pwd)/db:/app/db \
#              -v $(pwd)/data:/app/data \
#              petmaxi-dashboard:v8
#
# Notes
#   * /app/db and /app/data are mounted as volumes so a container restart
#     keeps SQLite snapshots and the vendas workbook. Delete the mounts for
#     a stateless demo run — the startup fetch will seed them from NETPET.
#   * NETPET TLS: by default the image uses truststore to trust the OS
#     cert store. On a non-domain host, set NETPET_CA_BUNDLE to a mounted
#     petMaxi-CA PEM.  For the nonprod demo VPN only, set NETPET_INSECURE=1.
#   * Dashboard is served at http://<host>:5000/petmaxi-dashboard/
#     (override with PETMAXI_BASE_PATH).

FROM python:3.11-slim AS runtime

# --- OS deps -----------------------------------------------------------------
# build-essential + libgomp for statsmodels / scikit-learn wheels on arm/debian,
# tini for clean PID-1 signal handling, ca-certificates kept fresh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libgomp1 \
        tini \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# --- Python deps -------------------------------------------------------------
# Copy only requirements first so pip layer caches across code changes.
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# --- App code ----------------------------------------------------------------
# Copy the rest. .dockerignore excludes __pycache__, db-wal/shm, etc.
COPY . /app

# --- Runtime config ----------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PETMAXI_DB_PATH=/app/db/petmaxi_v7.db \
    PETMAXI_INVENTORY_DB_PATH=/app/db/petmaxi_inventory.db \
    PETMAXI_ORDERS_DB_PATH=/app/db/petmaxi_open_orders.db \
    PETMAXI_LOG_DB=/app/db/petmaxi_errors.db \
    PETMAXI_DATA_PATH=/app/data/vendas_1_1.xlsx \
    PETMAXI_ORDERS_FIXTURE=/app/data/open_orders_fixture.json \
    PETMAXI_BASE_PATH=/petmaxi-dashboard \
    NETPET_BASE_URL=https://netpet.petmaxi.local \
    NETPET_HTTP_TIMEOUT=30

# Expose the Flask listener. Compose / k8s will map the host port.
EXPOSE 5000

# Lightweight health probe hits the batch status endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
                   base=os.environ.get('PETMAXI_BASE_PATH','/petmaxi-dashboard'); \
                   urllib.request.urlopen(f'http://127.0.0.1:5000{base}/api/batch_status', timeout=4); \
                   sys.exit(0)" || exit 1

# Clean PID-1 so SIGTERM reaches Flask / werkzeug promptly.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Dev-grade entry: keeps the startup NETPET fetches in __main__ (fixture
# fallback etc.) which gunicorn would skip. Swap to the gunicorn line below
# for a production host; the startup hooks must then be moved to an app
# factory or run as an init container.
CMD ["python", "dashboard_backend_v8.py"]
# CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", \
#      "--timeout", "120", "dashboard_backend_v8:app"]
