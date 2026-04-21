import argparse
import time
import threading
import random
import os
import psycopg2
from faker import Faker
from prometheus_client import start_http_server, Counter, Gauge

# Prometheus metrics for the simulator itself
metrics_inserts_total = Counter('simulator_inserts_total', 'Total number of patients inserted')
metrics_active_threads = Gauge('simulator_active_threads', 'Current number of active simulation threads')

fake = Faker()

class SimState:
    """Holds shared state for the worker threads so rate can be dynamically adjusted."""
    def __init__(self, inserts_per_sec):
        self.inserts_per_sec = inserts_per_sec

def get_db_connection():
    """
    Establish and return a connection to the PostgreSQL database.
    Fallback to 'localhost' if environment variables are missing, which is useful for local testing.
    """
    host = os.environ.get('DB_HOST', 'localhost')
    port = os.environ.get('DB_PORT', '5432')
    user = os.environ.get('DB_USER', 'hospital_admin')
    password = os.environ.get('DB_PASSWORD', 'er_secure_pass')
    dbname = os.environ.get('DB_NAME', 'hospital_db')

    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=dbname
        )
        return conn
    except Exception as e:
        print(f"Error connecting to DB: {e}")
        return None

def worker(stop_event, state):
    """
    A single worker thread that continuously inserts ER patients into the DB.
    simulating incoming patient registration at the ER.
    """
    conn = get_db_connection()
    if not conn:
        return

    metrics_active_threads.inc()
    try:
        with conn.cursor() as cur:
            while not stop_event.is_set():
                inserts_this_second = state.inserts_per_sec
                if inserts_this_second > 0:
                    for _ in range(inserts_this_second):
                        # Randomize patient data
                        patient_name = fake.name()
                        severity = random.randint(1, 5)

                        # Insert a new patient into er_patients
                        cur.execute(
                            "INSERT INTO er_patients (patient_name, arrival_time, severity, status) "
                            "VALUES (%s, NOW(), %s, 'waiting')",
                            (patient_name, severity)
                        )
                        metrics_inserts_total.inc()

                    # Commit the batch
                    conn.commit()

                # Sleep to maintain the target rate (roughly 1 second per loop)
                time.sleep(1.0)
    except Exception as e:
        print(f"Thread error: {e}")
    finally:
        metrics_active_threads.dec()
        if conn:
            conn.close()

def run_simulation(mode):
    """
    Manages the simulation state based on the selected mode: normal, spike, or cooldown.
    """
    start_time = time.time()
    threads = []
    stop_events = []

    # Initialize constraints based on mode
    if mode == 'normal':
        target_threads = 5
        state = SimState(1)
        print("Starting NORMAL mode: 5 threads, 1 insert/sec each.")
    elif mode == 'spike':
        target_threads = 50
        state = SimState(10)
        print("Starting SPIKE mode: 50 threads, 10 inserts/sec each.")
    elif mode == 'cooldown':
        target_threads = 50 # Start at 50 to simulate the exact peak before dropping
        state = SimState(10)
        print("Starting COOLDOWN mode: 50 threads descending back to 5 over 60 seconds.")
    else:
        print("Invalid mode.")
        return

    # Start initial batch of threads
    for _ in range(target_threads):
        ev = threading.Event()
        t = threading.Thread(target=worker, args=(ev, state))
        t.daemon = True
        threads.append(t)
        stop_events.append(ev)
        t.start()

    try:
        while True:
            elapsed = time.time() - start_time
            active = sum(1 for t in threads if t.is_alive())

            # Print current stats to stdout
            total_rate = active * state.inserts_per_sec
            print(f"Elapsed: {elapsed:.1f}s | Active Threads: {active} | Inserts/sec: {total_rate}")

            # Cooldown dynamic scaling logic
            if mode == 'cooldown':
                if elapsed <= 60:
                    # Linearly reduce threads from 50 to 5 over 60 seconds
                    progress = elapsed / 60.0
                    desired_threads = max(5, int(50 - (45 * progress)))

                    # Signal extra threads to stop
                    while active > desired_threads:
                        for ev in stop_events:
                            if not ev.is_set():
                                ev.set()
                                active -= 1
                                break

                    # Smoothly reduce inserts/sec for remaining threads down to 1
                    state.inserts_per_sec = max(1, int(10 - (9 * progress)))
                else:
                    # After 60 seconds, lock into normal baseline behavior
                    state.inserts_per_sec = 1

            time.sleep(2)

    except KeyboardInterrupt:
        print("\nStopping simulation threads...")
        for ev in stop_events:
            ev.set()
        print("Simulation stopped gracefully.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="ER Traffic Load Simulator")
    parser.add_argument('--mode', choices=['normal', 'spike', 'cooldown'], default='normal',
                        help="Mode of the simulation: normal, spike, or cooldown")
    parser.add_argument('--port', type=int, default=8080,
                        help="Port to expose Prometheus metrics")
    args = parser.parse_args()

    # Start Prometheus metrics server in a background daemon thread
    print(f"Starting simulator metrics server on port {args.port}")
    start_http_server(args.port)

    # Run the main simulation loop
    run_simulation(args.mode)
