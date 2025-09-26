# Main orchestrator Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y git curl redis-tools gnupg2 && \
    rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (placeholder - would need actual installation method)
# For now, we'll simulate Claude Code responses
# RUN curl -sSL https://claude.ai/install.sh | bash

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create orchestrator data directories (new structure)
RUN mkdir -p orchestrator_data/state/checkpoints orchestrator_data/handoffs orchestrator_data/logs orchestrator_data/metrics

# Create projects directory for mounted repositories
RUN mkdir -p projects

# Set up Git (will be configured via environment and mounted config)
RUN git config --global user.name "Orchestrator Bot" && \
    git config --global user.email "orchestrator@example.com"

# Ensure Python path includes the app directory
ENV PYTHONPATH=/app

# Default command
CMD ["python", "main.py"]