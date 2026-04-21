"""
Dashboard backend — queries Prometheus and serves the static frontend.
"""
import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import httpx

app = FastAPI(title="Kube-Health Dashboard")

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-service.kube-health.svc.cluster.local:9090")

# ---------------------------------------------------------------------------
# Prometheus query helper
# ---------------------------------------------------------------------------

async def prom_query(query: str) -> dict:
    url = f"{PROMETHEUS_URL}/api/v1/query"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, params={"query": query})
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != "success":
        raise HTTPException(status_code=502, detail=f"Prometheus error: {data}")
    return data["data"]["result"]


async def prom_range(query: str, duration: str = "10m", step: str = "15s") -> dict:
    import time
    end = int(time.time())
    start = end - _duration_to_seconds(duration)
    url = f"{PROMETHEUS_URL}/api/v1/query_range"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params={"query": query, "start": start, "end": end, "step": step})
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != "success":
        raise HTTPException(status_code=502, detail=f"Prometheus error: {data}")
    return data["data"]["result"]


def _duration_to_seconds(d: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    return int(d[:-1]) * units[d[-1]]


def _first_value(result: list) -> float | None:
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/metrics")
async def get_metrics():
    """Current snapshot of all key metrics."""
    active_connections = _first_value(await prom_query(
        'sum(pg_stat_activity_count{datname="hospital_db"})'
    ))
    db_up = _first_value(await prom_query("pg_up"))
    simulator_threads = _first_value(await prom_query("simulator_active_threads"))
    inserts_total = _first_value(await prom_query("simulator_inserts_total"))
    insert_rate = _first_value(await prom_query("rate(simulator_inserts_total[1m])"))

    return {
        "active_connections": active_connections,
        "db_up": bool(db_up),
        "simulator_active_threads": simulator_threads,
        "inserts_total": inserts_total,
        "insert_rate_per_sec": round(insert_rate, 2) if insert_rate else None,
    }


@app.get("/api/metrics/history")
async def get_metrics_history():
    """Time-series data for the last 10 minutes — used by the dashboard charts."""
    connections_series = await prom_range(
        'sum(pg_stat_activity_count{datname="hospital_db"})'
    )
    insert_rate_series = await prom_range("rate(simulator_inserts_total[1m])")

    def to_series(result):
        if not result:
            return []
        values = result[0].get("values", [])
        return [{"ts": v[0], "value": float(v[1])} for v in values]

    return {
        "active_connections": to_series(connections_series),
        "insert_rate": to_series(insert_rate_series),
    }


@app.get("/api/hpa")
async def get_hpa():
    """Replica count time-series for the postgres deployment."""
    series = await prom_range(
        'kube_horizontalpodautoscaler_status_current_replicas{horizontalpodautoscaler="postgres-hpa",namespace="kube-health"}'
    )

    def to_series(result):
        if not result:
            return []
        values = result[0].get("values", [])
        return [{"ts": v[0], "value": int(float(v[1]))} for v in values]

    desired_series = await prom_range(
        'kube_horizontalpodautoscaler_status_desired_replicas{horizontalpodautoscaler="postgres-hpa",namespace="kube-health"}'
    )

    return {
        "current_replicas": to_series(series),
        "desired_replicas": to_series(desired_series),
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
