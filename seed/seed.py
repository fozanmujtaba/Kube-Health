"""
Seed script — generates 50,000 realistic ER patients over the past 6 months.

Distributions are modelled on real ER epidemiology:
  - Severity: heavily weighted toward 3–4 (moderate/low urgency)
  - Arrivals: peak 10 am–2 pm and 7–10 pm, quiet 3–7 am
  - Age: bimodal (young adults + elderly)
  - Status: based on how long ago the patient arrived
  - Wait times: inversely correlated with severity
"""
import os
import random
import psycopg2
from datetime import datetime, timedelta
from faker import Faker

fake = Faker()

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
DB = dict(
    host=os.environ.get("DB_HOST", "postgres-service.kube-health.svc.cluster.local"),
    port=int(os.environ.get("DB_PORT", 5432)),
    user=os.environ.get("DB_USER", "hospital_admin"),
    password=os.environ.get("DB_PASSWORD", "er_secure_pass"),
    dbname=os.environ.get("DB_NAME", "hospital_db"),
)

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------
COMPLAINTS = {
    1: ["Cardiac arrest", "Severe polytrauma", "Stroke / CVA", "Respiratory failure", "Anaphylaxis", "Septic shock"],
    2: ["Chest pain", "Shortness of breath", "Altered mental status", "Severe abdominal pain", "Major fracture", "Overdose"],
    3: ["Abdominal pain", "Back pain", "Headache", "Persistent vomiting", "High fever", "Dehydration", "Minor fracture", "Laceration requiring sutures"],
    4: ["Sprain / strain", "Minor laceration", "Ear pain", "UTI symptoms", "Rash / allergic reaction", "Minor burns", "Dental pain"],
    5: ["Cold / flu symptoms", "Sore throat", "Minor bruising", "Insect bite", "Prescription refill", "Anxiety / panic attack"],
}

# Severity weights: 1=critical rare, 3=most common
SEVERITY_WEIGHTS = [5, 15, 35, 30, 15]

GENDERS = ["M", "F", "F", "M", "M", "F", "Other"]  # slight female majority

# Wait time ranges (minutes) per severity — higher severity = faster treatment
WAIT_RANGES = {1: (0, 5), 2: (5, 20), 3: (20, 60), 4: (60, 150), 5: (120, 300)}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bimodal_age():
    """Young adults (18–40) or elderly (60–90), with some middle-aged."""
    bucket = random.choices(["young", "middle", "elderly"], weights=[40, 30, 30])[0]
    if bucket == "young":    return random.randint(18, 40)
    if bucket == "middle":   return random.randint(41, 59)
    return random.randint(60, 90)


def arrival_time_in_past(days_back=180):
    """
    Random timestamp in the past `days_back` days, biased toward peak ER hours
    (10 am–2 pm and 7–10 pm) and weekdays.
    """
    base = datetime.now() - timedelta(days=random.uniform(0, days_back))
    # Pick an hour with realistic ER weighting
    hour_weights = [
        2, 1, 1, 1, 1, 2,   # 0–5  (night — quiet)
        4, 6, 8, 9, 10, 10,  # 6–11 (morning ramp)
        10, 10, 9, 8, 7, 8,  # 12–17 (afternoon)
        10, 10, 9, 7, 5, 3,  # 18–23 (evening peak then decline)
    ]
    hour = random.choices(range(24), weights=hour_weights)[0]
    minute = random.randint(0, 59)
    return base.replace(hour=hour, minute=minute, second=random.randint(0, 59), microsecond=0)


def derive_status_and_times(arrival: datetime, severity: int, wait_minutes: int):
    """
    Derive status and discharge_time based on how long ago the patient arrived.
    Recent arrivals are still waiting; old arrivals are discharged.
    """
    age_hours = (datetime.now() - arrival).total_seconds() / 3600
    treatment_duration = max(30, 120 - (severity - 1) * 20)  # critical = shorter stay

    if age_hours < wait_minutes / 60:
        return "waiting", None
    elif age_hours < (wait_minutes + treatment_duration) / 60:
        return "in_treatment", None
    else:
        discharge = arrival + timedelta(minutes=wait_minutes + treatment_duration + random.randint(0, 30))
        return "discharged", discharge


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def seed(n=50_000, batch=500):
    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    cur = conn.cursor()

    print(f"Seeding {n:,} patients in batches of {batch}…")
    rows = []

    for i in range(n):
        severity  = random.choices(range(1, 6), weights=SEVERITY_WEIGHTS)[0]
        arrival   = arrival_time_in_past()
        wait_min  = random.randint(*WAIT_RANGES[severity])
        status, discharge = derive_status_and_times(arrival, severity, wait_min)

        rows.append((
            fake.name(),
            arrival,
            severity,
            status,
            bimodal_age(),
            random.choice(GENDERS),
            random.choice(COMPLAINTS[severity]),
            wait_min,
            discharge,
        ))

        if len(rows) == batch:
            cur.executemany(
                """INSERT INTO er_patients
                   (patient_name, arrival_time, severity, status,
                    age, gender, chief_complaint, wait_time_minutes, discharge_time)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
            conn.commit()
            rows = []
            print(f"  {i+1:,} / {n:,}", end="\r", flush=True)

    if rows:
        cur.executemany(
            """INSERT INTO er_patients
               (patient_name, arrival_time, severity, status,
                age, gender, chief_complaint, wait_time_minutes, discharge_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            rows,
        )
        conn.commit()

    cur.close()
    conn.close()
    print(f"\nDone — {n:,} patients inserted.")


if __name__ == "__main__":
    seed()
