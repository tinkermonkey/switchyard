FROM python:3.11-slim

WORKDIR /app/fix-orchestrator

# Install system dependencies
RUN apt-get update && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service files
COPY . .

# Add orchestrator code to path for shared imports
ENV PYTHONPATH=/app:$PYTHONPATH

CMD ["python", "main.py"]
