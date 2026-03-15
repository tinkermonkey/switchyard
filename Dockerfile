# Main orchestrator Dockerfile
FROM python:3.11-slim

# Accept docker GID as build arg (defaults to 984 for Linux, override for macOS)
ARG DOCKER_GID=984

WORKDIR /app

# Install Node.js and Claude CLI
ENV NODE_VERSION=v22.20.0
RUN apt update -y && apt install curl git redis-tools gnupg2 procps -y \
    && ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then NODE_ARCH="x64"; elif [ "$ARCH" = "arm64" ]; then NODE_ARCH="arm64"; else NODE_ARCH="x64"; fi \
    && curl -fsSL https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-$NODE_ARCH.tar.gz -o node.tar.gz \
    && tar -xzvf node.tar.gz && rm node.tar.gz \
    && cp -r node-$NODE_VERSION-linux-$NODE_ARCH/* /usr/local/ \
    && rm -rf node-$NODE_VERSION-linux-$NODE_ARCH \
    && npm install -g @anthropic-ai/claude-code @playwright/mcp \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright dependencies for MCP server (minimal - we'll use Browserless instead of local browsers)
RUN apt update -y && apt install -y \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
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

# Set up Git (will be configured via environment and mounted config)
# Use --system to set git config for all users (goes to /etc/gitconfig)
RUN git config --system user.name "Orchestrator Bot" && \
    git config --system user.email "orchestrator@example.com" && \
    git config --system --add safe.directory '*'

# Ensure Python path includes the app directory
ENV PYTHONPATH=/app

# Create docker group with host's docker GID and add orchestrator user to it
# This enables docker socket access for dev_environment_setup agent
# On macOS, DOCKER_GID=0 (root group), so we add user to root group
# On Linux, DOCKER_GID is typically 984-999, so we create a docker group
RUN if [ "${DOCKER_GID}" = "0" ]; then \
        useradd -m -u 1000 -G root orchestrator; \
    else \
        groupadd -g ${DOCKER_GID} docker || true && \
        useradd -m -u 1000 -G docker orchestrator; \
    fi && \
    mkdir -p /home/orchestrator/.ssh /home/orchestrator/.config && \
    chown -R orchestrator:orchestrator /home/orchestrator && \
    chmod 700 /home/orchestrator/.ssh /home/orchestrator/.config && \
    rm -rf /home/orchestrator/.gitconfig /home/orchestrator/.orchestrator

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# NEW: Ensure Claude Code wrapper is executable (for container-side Redis writes)
RUN chmod +x /app/scripts/docker-claude-wrapper.py

# Copy and set permissions for entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create orchestrator data directories (new structure) and set ownership
RUN mkdir -p orchestrator_data/state/checkpoints orchestrator_data/handoffs orchestrator_data/logs orchestrator_data/metrics && \
    mkdir -p projects && \
    chown -R orchestrator:orchestrator /app orchestrator_data projects

# Switch to non-root user
USER orchestrator

# Install Claude Code plugins (must be after USER orchestrator so they install to the right home dir)
RUN claude plugin marketplace add anthropics/claude-plugins-official && \
    claude plugin install pr-review-toolkit || true

# Set entrypoint to handle SSH setup at runtime
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default command
CMD ["python", "main.py"]