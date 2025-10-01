# Main orchestrator Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install Node.js and Claude CLI
ENV NODE_VERSION=v22.20.0
RUN apt update -y && apt install curl git redis-tools gnupg2 -y \
    && ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then NODE_ARCH="x64"; elif [ "$ARCH" = "arm64" ]; then NODE_ARCH="arm64"; else NODE_ARCH="x64"; fi \
    && curl -fsSL https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-$NODE_ARCH.tar.gz -o node.tar.gz \
    && tar -xzvf node.tar.gz && rm node.tar.gz \
    && cp -r node-$NODE_VERSION-linux-$NODE_ARCH/* /usr/local/ \
    && rm -rf node-$NODE_VERSION-linux-$NODE_ARCH \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Install Docker CLI (for dev_environment_setup agent to build images)
RUN curl -fsSL https://download.docker.com/linux/debian/gpg | \
    gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian bookworm stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Copy and set permissions for entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create orchestrator data directories (new structure)
RUN mkdir -p orchestrator_data/state/checkpoints orchestrator_data/handoffs orchestrator_data/logs orchestrator_data/metrics

# Create projects directory for mounted repositories
RUN mkdir -p projects

# Set up Git (will be configured via environment and mounted config)
RUN git config --global user.name "Orchestrator Bot" && \
    git config --global user.email "orchestrator@example.com"

# Ensure Python path includes the app directory
ENV PYTHONPATH=/app

# Create docker group with GID 999 (typical docker socket GID on host)
# and add orchestrator user to it for Docker socket access
RUN groupadd -g 999 docker || true && \
    useradd -m -u 1000 -G docker orchestrator && \
    chown -R orchestrator:orchestrator /app orchestrator_data projects && \
    mkdir -p /home/orchestrator/.ssh && \
    chown orchestrator:orchestrator /home/orchestrator/.ssh && \
    chmod 700 /home/orchestrator/.ssh

# Switch to non-root user
USER orchestrator

# Set entrypoint to handle SSH setup at runtime
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default command
CMD ["python", "main.py"]