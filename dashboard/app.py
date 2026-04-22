"""
Dashboard backend — queries Prometheus + PostgreSQL and serves the frontend.
"""
import os
import time
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import httpx

app = FastAPI(title="Kube-Health Dashboard")

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-service.kube-health.svc.cluster.local:9090")

DB_CONFIG = dict(
    host=os.environ.get("DB_HOST", "postgres-service.kube-health.svc.cluster.local"),
    port=int(os.environ.get("DB_PORT", 5432)),
    user=os.environ.get("DB_USER", "hospital_admin"),
    password=os.environ.get("DB_PASSWORD", "er_secure_pass"),
    dbname=os.environ.get("DB_NAME", "hospital_db"),
    connect_timeout=5,
)

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def db_query(sql: str, params=None) -> list[dict]:
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Prometheus helpers
# ---------------------------------------------------------------------------

async def prom_query(query: str):
    url = f"{PROMETHEUS_URL}/api/v1/query"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, params={"query": query})
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != "success":
        raise HTTPException(status_code=502, detail=f"Prometheus error: {data}")
    return data["data"]["result"]


async def prom_range(query: str, duration: str = "10m", step: str = "15s"):
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


def _to_series(result: list) -> list:
    if not result:
        return []
    return [{"ts": v[0], "value": float(v[1])} for v in result[0].get("values", [])]


# ---------------------------------------------------------------------------
# Prometheus API routes
# ---------------------------------------------------------------------------

@app.get("/api/metrics")
async def get_metrics():
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
    return {
        "active_connections": _to_series(await prom_range(
            'sum(pg_stat_activity_count{datname="hospital_db"})'
        )),
        "insert_rate": _to_series(await prom_range("rate(simulator_inserts_total[1m])")),
    }


@app.get("/api/hpa")
async def get_hpa():
    return {
        "current_replicas": _to_series(await prom_range(
            'kube_horizontalpodautoscaler_status_current_replicas{horizontalpodautoscaler="postgres-hpa",namespace="kube-health"}'
        )),
        "desired_replicas": _to_series(await prom_range(
            'kube_horizontalpodautoscaler_status_desired_replicas{horizontalpodautoscaler="postgres-hpa",namespace="kube-health"}'
        )),
    }


# ---------------------------------------------------------------------------
# Patient / DB API routes
# ---------------------------------------------------------------------------

@app.get("/api/patients/stats")
async def get_patient_stats():
    """Aggregate stats directly from PostgreSQL."""
    rows = db_query("""
        SELECT
            COUNT(*)                                                    AS total,
            COUNT(*) FILTER (WHERE status = 'waiting')                 AS waiting,
            COUNT(*) FILTER (WHERE status = 'in_treatment')            AS in_treatment,
            COUNT(*) FILTER (WHERE status = 'discharged')              AS discharged,
            ROUND(AVG(wait_time_minutes) FILTER (WHERE wait_time_minutes IS NOT NULL))::int
                                                                        AS avg_wait_minutes,
            ROUND(AVG(wait_time_minutes) FILTER (WHERE severity = 1))::int AS avg_wait_critical,
            ROUND(AVG(age) FILTER (WHERE age IS NOT NULL))::int         AS avg_age
        FROM er_patients
    """)
    return rows[0] if rows else {}


@app.get("/api/patients/severity")
async def get_severity_breakdown():
    """Count of patients grouped by severity level."""
    rows = db_query("""
        SELECT severity, COUNT(*) AS count
        FROM er_patients
        GROUP BY severity
        ORDER BY severity
    """)
    return rows


@app.get("/api/patients/hourly")
async def get_hourly_intake():
    """Average patient arrivals per hour-of-day (last 30 days) — shows ER peak patterns."""
    rows = db_query("""
        SELECT
            EXTRACT(HOUR FROM arrival_time)::int AS hour,
            COUNT(*) AS count
        FROM er_patients
        WHERE arrival_time >= NOW() - INTERVAL '30 days'
        GROUP BY 1
        ORDER BY 1
    """)
    return rows


@app.get("/api/patients/complaints")
async def get_top_complaints():
    """Top 10 chief complaints by volume."""
    rows = db_query("""
        SELECT chief_complaint, COUNT(*) AS count
        FROM er_patients
        WHERE chief_complaint IS NOT NULL
        GROUP BY chief_complaint
        ORDER BY count DESC
        LIMIT 10
    """)
    return rows


@app.get("/api/patients/recent")
async def get_recent_patients():
    """Last 15 patients admitted."""
    rows = db_query("""
        SELECT id, patient_name, age, gender, severity,
               chief_complaint, status, wait_time_minutes,
               arrival_time
        FROM er_patients
        ORDER BY arrival_time DESC
        LIMIT 15
    """)
    for r in rows:
        if r.get("arrival_time"):
            r["arrival_time"] = r["arrival_time"].isoformat()
    return rows


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
