#!/bin/bash
# Wrapper script for docker-compose that sets rootless Docker socket

# Detect if running rootless Docker
if [ -S "/run/user/$(id -u)/docker.sock" ]; then
    export COMPOSE_DOCKER_SOCK="/run/user/$(id -u)/docker.sock"
fi

# Pass all arguments to docker compose
exec docker compose "$@"
