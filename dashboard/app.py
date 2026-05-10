"""
Dashboard backend — queries Prometheus + PostgreSQL and serves the frontend.
Prometheus errors are handled gracefully so the app works without a K8s cluster.
"""
import os
import time
import random
import psycopg2
import psycopg2.extras
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import urlparse
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import httpx

# ---------------------------------------------------------------------------
# DB config — supports DATABASE_URL (Railway) or individual env vars
# ---------------------------------------------------------------------------

_db_url = os.environ.get("DATABASE_URL", "")
if _db_url:
    _p = urlparse(_db_url)
    DB_CONFIG = dict(
        host=_p.hostname,
        port=_p.port or 5432,
        user=_p.username,
        password=_p.password,
        dbname=_p.path.lstrip("/"),
        connect_timeout=10,
        sslmode="prefer",
    )
else:
    DB_CONFIG = dict(
        host=os.environ.get("DB_HOST", "postgres-service.kube-health.svc.cluster.local"),
        port=int(os.environ.get("DB_PORT", 5432)),
        user=os.environ.get("DB_USER", "hospital_admin"),
        password=os.environ.get("DB_PASSWORD", "er_secure_pass"),
        dbname=os.environ.get("DB_NAME", "hospital_db"),
        connect_timeout=10,
    )

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-service.kube-health.svc.cluster.local:9090")

# ---------------------------------------------------------------------------
# Simple TTL cache
# ---------------------------------------------------------------------------

_cache: dict = {}

def _cached(key: str, ttl: int, fn):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < ttl:
        return entry["val"]
    val = fn()
    _cache[key] = {"ts": time.time(), "val": val}
    return val

# ---------------------------------------------------------------------------
# DB helpers
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
# DB init + seed on startup
# ---------------------------------------------------------------------------

COMPLAINTS = {
    1: ["Cardiac arrest", "Severe polytrauma", "Stroke / CVA", "Respiratory failure", "Anaphylaxis"],
    2: ["Chest pain", "Shortness of breath", "Altered mental status", "Severe abdominal pain", "Overdose"],
    3: ["Abdominal pain", "Back pain", "Headache", "Persistent vomiting", "High fever", "Dehydration"],
    4: ["Sprain / strain", "Minor laceration", "Ear pain", "UTI symptoms", "Rash", "Dental pain"],
    5: ["Cold / flu symptoms", "Sore throat", "Minor bruising", "Insect bite", "Anxiety / panic attack"],
}
SEVERITY_WEIGHTS = [5, 15, 35, 30, 15]
WAIT_RANGES = {1: (0, 5), 2: (5, 20), 3: (20, 60), 4: (60, 150), 5: (120, 300)}
GENDERS = ["M", "F", "F", "M", "M", "F", "Other"]

FIRST_NAMES = ["James","Mary","John","Patricia","Robert","Jennifer","Michael","Linda",
               "William","Barbara","David","Susan","Richard","Jessica","Joseph","Sarah",
               "Thomas","Karen","Charles","Lisa","Christopher","Nancy","Daniel","Betty",
               "Matthew","Margaret","Anthony","Sandra","Mark","Ashley","Donald","Emily",
               "Steven","Dorothy","Paul","Kimberly","Andrew","Carol","Kenneth","Michelle"]
LAST_NAMES  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
               "Rodriguez","Martinez","Hernandez","Lopez","Gonzalez","Wilson","Anderson",
               "Thomas","Taylor","Moore","Jackson","Martin","Lee","Perez","Thompson","White",
               "Harris","Sanchez","Clark","Ramirez","Lewis","Robinson","Walker","Young"]

def _rand_name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"

def _bimodal_age():
    bucket = random.choices(["young", "middle", "elderly"], weights=[40, 30, 30])[0]
    if bucket == "young":  return random.randint(18, 40)
    if bucket == "middle": return random.randint(41, 59)
    return random.randint(60, 90)

def _arrival_time():
    base = datetime.now() - timedelta(days=random.uniform(0, 180))
    hour_weights = [2,1,1,1,1,2,4,6,8,9,10,10,10,10,9,8,7,8,10,10,9,7,5,3]
    hour = random.choices(range(24), weights=hour_weights)[0]
    return base.replace(hour=hour, minute=random.randint(0,59), second=random.randint(0,59), microsecond=0)

def _derive_status(arrival, severity, wait_min):
    age_h = (datetime.now() - arrival).total_seconds() / 3600
    treat_dur = max(30, 120 - (severity - 1) * 20)
    if age_h < wait_min / 60:
        return "waiting", None
    elif age_h < (wait_min + treat_dur) / 60:
        return "in_treatment", None
    else:
        discharge = arrival + timedelta(minutes=wait_min + treat_dur + random.randint(0, 30))
        return "discharged", discharge

def _seed_patients(n=3000):
    with get_db() as conn:
        with conn.cursor() as cur:
            batch = []
            for _ in range(n):
                sev     = random.choices(range(1, 6), weights=SEVERITY_WEIGHTS)[0]
                arrival = _arrival_time()
                wait    = random.randint(*WAIT_RANGES[sev])
                status, discharge = _derive_status(arrival, sev, wait)
                batch.append((
                    _rand_name(), arrival, sev, status,
                    _bimodal_age(), random.choice(GENDERS),
                    random.choice(COMPLAINTS[sev]), wait, discharge,
                ))
                if len(batch) == 500:
                    cur.executemany(
                        """INSERT INTO er_patients
                           (patient_name,arrival_time,severity,status,age,gender,
                            chief_complaint,wait_time_minutes,discharge_time)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        batch,
                    )
                    conn.commit()
                    batch = []
            if batch:
                cur.executemany(
                    """INSERT INTO er_patients
                       (patient_name,arrival_time,severity,status,age,gender,
                        chief_complaint,wait_time_minutes,discharge_time)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    batch,
                )
                conn.commit()
    print(f"Seeded {n} patients.")

def _init_db():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS er_patients (
                        id               SERIAL PRIMARY KEY,
                        patient_name     TEXT NOT NULL,
                        arrival_time     TIMESTAMP NOT NULL DEFAULT NOW(),
                        severity         INT NOT NULL CHECK (severity BETWEEN 1 AND 5),
                        status           TEXT NOT NULL DEFAULT 'waiting',
                        age              INT,
                        gender           TEXT,
                        chief_complaint  TEXT,
                        wait_time_minutes INT,
                        discharge_time   TIMESTAMP
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_er_patients_arrival ON er_patients (arrival_time DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_er_patients_severity ON er_patients (severity)")
                conn.commit()
                cur.execute("SELECT COUNT(*) FROM er_patients")
                count = cur.fetchone()[0]
        print(f"DB ready — {count} existing rows.")
        if count < 100:
            print("Seeding database…")
            _seed_patients(3000)
        else:
            print(f"Skipping seed, {count} rows already present.")
    except Exception as e:
        print(f"DB init error: {e}")

# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    yield

app = FastAPI(title="Kube-Health Dashboard", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Prometheus helpers — return [] on any error (Prometheus may not be available)
# ---------------------------------------------------------------------------

async def prom_query(query: str):
    try:
        url = f"{PROMETHEUS_URL}/api/v1/query"
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url, params={"query": query})
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "success":
            return []
        return data["data"]["result"]
    except Exception:
        return []


async def prom_range(query: str, duration: str = "10m", step: str = "15s"):
    try:
        end = int(time.time())
        start = end - _duration_to_seconds(duration)
        url = f"{PROMETHEUS_URL}/api/v1/query_range"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params={"query": query, "start": start, "end": end, "step": step})
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "success":
            return []
        return data["data"]["result"]
    except Exception:
        return []


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

    # When Prometheus is unavailable, check the actual DB connection
    if db_up is None:
        try:
            db_query("SELECT 1")
            db_up = True
        except Exception:
            db_up = False

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
    return _cached("stats", 15, lambda: db_query("""
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
    """)[0] or {})


@app.get("/api/patients/severity")
async def get_severity_breakdown():
    return _cached("severity", 15, lambda: db_query("""
        SELECT severity, COUNT(*) AS count
        FROM er_patients
        GROUP BY severity
        ORDER BY severity
    """))


@app.get("/api/patients/hourly")
async def get_hourly_intake():
    return _cached("hourly", 60, lambda: db_query("""
        SELECT
            EXTRACT(HOUR FROM arrival_time)::int AS hour,
            COUNT(*) AS count
        FROM er_patients
        WHERE arrival_time >= NOW() - INTERVAL '30 days'
        GROUP BY 1
        ORDER BY 1
    """))


@app.get("/api/patients/complaints")
async def get_top_complaints():
    return _cached("complaints", 30, lambda: db_query("""
        SELECT chief_complaint, COUNT(*) AS count
        FROM er_patients
        WHERE chief_complaint IS NOT NULL
        GROUP BY chief_complaint
        ORDER BY count DESC
        LIMIT 10
    """))


@app.get("/api/patients/recent")
async def get_recent_patients():
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
