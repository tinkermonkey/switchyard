#!/bin/bash
# Wrapper script for docker-compose that sets environment variables for Docker access

# Set USER_UID/USER_GID to match host user (for file permissions in mounted volumes)
# Note: Can't use UID/GID as they are readonly in bash
export USER_UID=$(id -u)
export USER_GID=$(id -g)

# Detect if running rootless Docker
if [ -S "/run/user/$(id -u)/docker.sock" ]; then
    export COMPOSE_DOCKER_SOCK="/run/user/$(id -u)/docker.sock"
    # For rootless Docker, socket appears as root:984 inside container due to user namespace mapping
    # We need to add orchestrator user to group 984
    export DOCKER_GID=984
else
    # For system Docker, get the docker group GID
    export COMPOSE_DOCKER_SOCK="/var/run/docker.sock"
    export DOCKER_GID=$(getent group docker | cut -d: -f3 || echo 984)
fi

echo "Using Docker socket: $COMPOSE_DOCKER_SOCK with GID: $DOCKER_GID"
echo "Container will run as UID: $USER_UID, GID: $USER_GID"

# Pass all arguments to docker compose
exec docker compose "$@"
