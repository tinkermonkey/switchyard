---
invoked_by: prompts/context.py — PromptContext._build_docker_socket_section()
  Appended to agent prompts when docker_socket_access is enabled for the agent in project config.
---
## Docker Socket Access

The Docker socket is mounted into your container at `/var/run/docker.sock`. You can use `docker` and `docker-compose` commands directly.

**Networking**

Your container runs on the `switchyard_orchestrator-net` bridge network. Containers you start will be on the host Docker daemon's default network by default and won't be reachable from your container unless you explicitly connect them:

```bash
# Start a service and attach it to the shared network
docker run --network switchyard_orchestrator-net ...

# Or with docker-compose — add a networks section:
# networks:
#   default:
#     name: switchyard_orchestrator-net
#     external: true
```

**Workspace path**

Your project workspace is mounted at `/workspace`. Containers you launch should mount the same path if they need access to project files:

```bash
docker run --network switchyard_orchestrator-net -v /workspace:/workspace ...
```

**docker-compose**

When running `docker-compose up`, services will be accessible by their service name as the hostname, provided they share a network with your container. Use `docker-compose exec` or `docker-compose run` to interact with them after startup.

**Cleanup**

Remove any containers, networks, or volumes you create before your task completes. Use `docker-compose down -v` or explicit `docker rm`/`docker network rm` calls to avoid leaving orphaned resources.

**Port allocation**

Other agents with Docker socket access may be running test infrastructure against this same project concurrently. Never publish a fixed host port:

```bash
# WRONG -- fixed host port, will collide with another concurrent run
docker run -p 5432:5432 postgres

# RIGHT -- let Docker assign an ephemeral host port
docker run -P postgres            # publishes every EXPOSEd port to a random host port
docker run -p 127.0.0.1::5432 postgres   # or explicitly randomize one port
```

If you're using testcontainers (or an equivalent library) to launch test fixtures, always let it assign ephemeral host ports rather than pinning one — e.g. Testcontainers' `with_exposed_ports()` / dynamic port mapping, resolved via the library's `get_exposed_port()`-style lookup after startup, in whatever language/framework you're using. Never hardcode a host port for a container the library manages.

If you're using docker-compose for test infrastructure, omit static host ports from `ports:` entries, or bind to port `0` to force ephemeral assignment:

```yaml
services:
  postgres:
    image: postgres
    ports:
      - "5432"        # ephemeral host port, fixed container port
      # or: "0:5432"
```
