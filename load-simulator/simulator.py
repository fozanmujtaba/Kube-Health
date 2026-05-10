import argparse
import time
import threading
import random
import os
import psycopg2
from faker import Faker
from prometheus_client import start_http_server, Counter, Gauge

metrics_inserts_total = Counter('simulator_inserts_total', 'Total number of patients inserted')
metrics_active_threads = Gauge('simulator_active_threads', 'Current number of active simulation threads')

fake = Faker()

COMPLAINTS = {
    1: ["Cardiac arrest", "Severe trauma", "Stroke / CVA", "Respiratory failure", "Anaphylaxis"],
    2: ["Chest pain", "Shortness of breath", "Altered mental status", "Severe abdominal pain", "Overdose"],
    3: ["Abdominal pain", "Back pain", "Headache", "Persistent vomiting", "High fever", "Minor fracture"],
    4: ["Sprain / strain", "Minor laceration", "Ear pain", "UTI symptoms", "Rash", "Dental pain"],
    5: ["Cold / flu symptoms", "Sore throat", "Minor bruising", "Insect bite", "Anxiety / panic attack"],
}
WAIT_RANGES = {1: (0, 5), 2: (5, 20), 3: (20, 60), 4: (60, 150), 5: (120, 300)}
GENDERS = ["M", "F", "F", "M", "F", "M", "Other"]


class SimState:
    def __init__(self, inserts_per_sec):
        self.inserts_per_sec = inserts_per_sec


def get_db_connection():
    host = os.environ.get('DB_HOST', 'localhost')
    port = os.environ.get('DB_PORT', '5432')
    user = os.environ.get('DB_USER', 'hospital_admin')
    password = os.environ.get('DB_PASSWORD', 'er_secure_pass')
    dbname = os.environ.get('DB_NAME', 'hospital_db')
    try:
        conn = psycopg2.connect(
            host=host, port=port, user=user,
            password=password, dbname=dbname, connect_timeout=5
        )
        return conn
    except Exception as e:
        print(f"DB connect error: {e}")
        return None


def worker(stop_event, state):
    metrics_active_threads.inc()
    try:
        while not stop_event.is_set():
            conn = get_db_connection()
            if not conn:
                time.sleep(3)
                continue
            try:
                with conn.cursor() as cur:
                    while not stop_event.is_set():
                        inserts_this_second = state.inserts_per_sec
                        if inserts_this_second > 0:
                            for _ in range(inserts_this_second):
                                severity = random.randint(1, 5)
                                age = random.choices(
                                    range(18, 91),
                                    weights=[2 if a < 40 or a > 65 else 1 for a in range(18, 91)]
                                )[0]
                                wait_min = random.randint(*WAIT_RANGES[severity])
                                cur.execute(
                                    """INSERT INTO er_patients
                                       (patient_name, arrival_time, severity, status,
                                        age, gender, chief_complaint, wait_time_minutes)
                                       VALUES (%s, NOW(), %s, 'waiting', %s, %s, %s, %s)""",
                                    (fake.name(), severity, age,
                                     random.choice(GENDERS),
                                     random.choice(COMPLAINTS[severity]),
                                     wait_min)
                                )
                                metrics_inserts_total.inc()
                            conn.commit()
                        time.sleep(1.0)
            except Exception as e:
                print(f"Thread DB error (will reconnect): {e}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    finally:
        metrics_active_threads.dec()


def spawn_threads(n, state):
    stop_events = []
    threads = []
    for _ in range(n):
        ev = threading.Event()
        t = threading.Thread(target=worker, args=(ev, state), daemon=True)
        stop_events.append(ev)
        threads.append(t)
        t.start()
    return threads, stop_events


def stop_n_threads(stop_events, n):
    stopped = 0
    for ev in stop_events:
        if stopped >= n:
            break
        if not ev.is_set():
            ev.set()
            stopped += 1


def run_simulation(mode):
    start_time = time.time()

    if mode == 'normal':
        state = SimState(1)
        threads, stop_events = spawn_threads(5, state)
        print("NORMAL mode: 5 threads × 1 insert/sec")
        while True:
            elapsed = time.time() - start_time
            active = sum(1 for t in threads if t.is_alive())
            print(f"Elapsed: {elapsed:.1f}s | Active Threads: {active} | Inserts/sec: {active * state.inserts_per_sec}")
            time.sleep(2)

    elif mode == 'spike':
        state = SimState(10)
        threads, stop_events = spawn_threads(50, state)
        print("SPIKE mode: 50 threads × 10 inserts/sec")
        while True:
            elapsed = time.time() - start_time
            active = sum(1 for t in threads if t.is_alive())
            print(f"Elapsed: {elapsed:.1f}s | Active Threads: {active} | Inserts/sec: {active * state.inserts_per_sec}")
            time.sleep(2)

    elif mode == 'cooldown':
        state = SimState(10)
        threads, stop_events = spawn_threads(50, state)
        print("COOLDOWN mode: ramp down from 50 → 5 threads over 60s")
        while True:
            elapsed = time.time() - start_time
            active = sum(1 for t in threads if t.is_alive())
            if elapsed <= 60:
                progress = elapsed / 60.0
                desired = max(5, int(50 - 45 * progress))
                if active > desired:
                    stop_n_threads(stop_events, active - desired)
                state.inserts_per_sec = max(1, int(10 - 9 * progress))
            else:
                state.inserts_per_sec = 1
            print(f"Elapsed: {elapsed:.1f}s | Active Threads: {active} | Inserts/sec: {active * state.inserts_per_sec}")
            time.sleep(2)

    elif mode == 'cycle':
        # Continuously loops: normal (90s) → spike (90s) → cooldown (60s) → repeat
        print("CYCLE mode: normal → spike → cooldown → repeat")
        phases = [
            ('normal',   90,  5, SimState(1)),
            ('spike',    90, 50, SimState(10)),
            ('cooldown', 60, 50, SimState(10)),
        ]
        while True:
            for phase_name, duration, n_threads, state in phases:
                print(f"\n=== Phase: {phase_name.upper()} ({duration}s) ===")
                threads, stop_events = spawn_threads(n_threads, state)
                phase_start = time.time()

                while time.time() - phase_start < duration:
                    elapsed_phase = time.time() - phase_start
                    active = sum(1 for t in threads if t.is_alive())

                    if phase_name == 'cooldown':
                        progress = min(1.0, elapsed_phase / duration)
                        desired = max(5, int(n_threads - (n_threads - 5) * progress))
                        if active > desired:
                            stop_n_threads(stop_events, active - desired)
                        state.inserts_per_sec = max(1, int(10 - 9 * progress))

                    print(f"[{phase_name}] {elapsed_phase:.0f}s | Threads: {active} | Inserts/sec: {active * state.inserts_per_sec}")
                    time.sleep(2)

                # Stop all threads from this phase before next phase
                for ev in stop_events:
                    ev.set()
                for t in threads:
                    t.join(timeout=3)
    else:
        print("Invalid mode.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="ER Traffic Load Simulator")
    parser.add_argument('--mode', choices=['normal', 'spike', 'cooldown', 'cycle'], default='cycle',
                        help="Simulation mode (default: cycle)")
    parser.add_argument('--port', type=int, default=8080,
                        help="Port for Prometheus metrics")
    args = parser.parse_args()

    print(f"Starting simulator metrics server on port {args.port}")
    start_http_server(args.port)
    run_simulation(args.mode)
