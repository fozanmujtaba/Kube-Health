# Kube-Health

A Kubernetes demo that simulates an Emergency Room (ER) patient registration system with autoscaling driven by real database load.

## Architecture

```
Load Simulator ──inserts──► PostgreSQL (2–10 replicas)
                                  │
                          postgres-exporter
                                  │
                             Prometheus
                                  │
                    ┌─────────────┴──────────────┐
                    │                            │
          Prometheus Adapter              Dashboard (FastAPI)
                    │                            │
                   HPA                     Browser UI
```

## Components

| Component | Description | Port |
|---|---|---|
| PostgreSQL 15 | Hospital ER patient database, scaled by HPA | 5432 |
| postgres-exporter | Exports DB metrics to Prometheus | 9187 |
| Prometheus | Scrapes and stores all metrics | 9090 (NodePort 30090) |
| Prometheus Adapter | Bridges Prometheus → K8s custom metrics API | — |
| HPA | Scales postgres 2–10 replicas on connections | — |
| Load Simulator | Generates ER patient traffic (normal/spike/cooldown) | 8080 |
| Dashboard | Real-time UI showing DB load and replica count | 5000 (NodePort 30050) |

## Prerequisites

- [minikube](https://minikube.sigs.k8s.io/) or any K8s cluster
- `kubectl` configured
- Docker (to build images)

## Quick Start

### 1. Start minikube

```bash
minikube start --cpus=4 --memory=6g
eval $(minikube docker-env)   # use minikube's Docker daemon
```

### 2. Build Docker images

```bash
# Load Simulator
docker build -t kube-health/load-simulator:latest ./load-simulator

# Dashboard
docker build -t kube-health/dashboard:latest ./dashboard
```

### 3. Deploy everything

```bash
# Core infrastructure
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/postgres-secret.yaml
kubectl apply -f k8s/postgres-configmap.yaml
kubectl apply -f k8s/postgres-pvc.yaml
kubectl apply -f k8s/postgres-deployment.yaml
kubectl apply -f k8s/postgres-service.yaml

# Monitoring
kubectl apply -f k8s/postgres-exporter-deployment.yaml
kubectl apply -f k8s/postgres-exporter-service.yaml
kubectl apply -f k8s/prometheus-configmap.yaml
kubectl apply -f k8s/prometheus-deployment.yaml
kubectl apply -f k8s/prometheus-service.yaml

# Custom metrics & HPA
kubectl apply -f k8s/prometheus-adapter/adapter-config.yaml
kubectl apply -f k8s/prometheus-adapter/adapter-deployment.yaml
kubectl apply -f k8s/hpa.yaml

# Load simulator
kubectl apply -f k8s/load-simulator-deployment.yaml
kubectl apply -f k8s/load-simulator-service.yaml

# Dashboard
kubectl apply -f k8s/dashboard-deployment.yaml
kubectl apply -f k8s/dashboard-service.yaml
```

### 4. Verify everything is running

```bash
kubectl get pods -n kube-health
kubectl get hpa -n kube-health
```

### 5. Open the Dashboard

```bash
minikube service dashboard-service -n kube-health
# or
kubectl port-forward svc/dashboard-service 5000:5000 -n kube-health
# then open http://localhost:5000
```

### 6. Open Prometheus (optional)

```bash
minikube service prometheus-service -n kube-health
# NodePort 30090 → http://<minikube-ip>:30090
```

## Running Load Scenarios

The simulator runs in `normal` mode by default. To switch modes, update the `args` in [k8s/load-simulator-deployment.yaml](k8s/load-simulator-deployment.yaml):

| Mode | Behaviour | Effect on HPA |
|---|---|---|
| `normal` | 5 threads × 1 insert/sec | Stays at 2 replicas |
| `spike` | 50 threads × 10 inserts/sec | Triggers scale-up to ~8–10 replicas |
| `cooldown` | Ramps down from spike over 60s | Replicas gradually scale back down |

```bash
# Switch to spike mode
kubectl set env deployment/load-simulator -n kube-health MODE=spike
# Or edit the deployment and change --mode spike, then:
kubectl rollout restart deployment/load-simulator -n kube-health
```

## Autoscaling Logic

The HPA scales the `postgres` deployment based on two metrics:

1. **CPU utilization** — target 50% of requested CPU per pod
2. **`hospital_active_connections`** — custom metric (from Prometheus via Prometheus Adapter), target average of 20 connections per pod

The Prometheus Adapter maps `pg_stat_activity_count` → `hospital_active_connections` so the HPA can consume it natively.

## CI/CD

GitHub Actions (`.github/workflows/build.yml`) builds and pushes Docker images to Docker Hub on every push to `main`. Add these secrets to your repo:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

## Project Structure

```
kube-health/
├── k8s/                        # Kubernetes manifests
│   ├── namespace.yaml
│   ├── postgres-*.yaml         # Database (secret, configmap, pvc, deployment, service)
│   ├── prometheus-*.yaml       # Monitoring (configmap, deployment, service)
│   ├── postgres-exporter-*.yaml
│   ├── load-simulator-*.yaml   # Load generator (deployment, service)
│   ├── dashboard-*.yaml        # Dashboard (deployment, service)
│   ├── hpa.yaml
│   └── prometheus-adapter/     # Custom metrics bridge
├── load-simulator/             # Python ER traffic simulator
│   ├── simulator.py
│   ├── Dockerfile
│   └── requirements.txt
├── dashboard/                  # FastAPI backend + HTML frontend
│   ├── app.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── static/index.html
└── .github/workflows/
    └── build.yml               # CI/CD pipeline
```
