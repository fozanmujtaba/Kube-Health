FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for psycopg2 compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy simulator source code
COPY simulator.py .

# Expose prometheus metrics port
EXPOSE 8080

# Default entrypoint runs the simulator
ENTRYPOINT ["python", "simulator.py"]
