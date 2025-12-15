---
name: deployment-expert
description: Expert in deployment automation, CI/CD pipelines, Docker, Kubernetes, and cloud deployments. Use when deploying applications, setting up CI/CD, containerizing apps, or managing infrastructure. Supports Docker, Kubernetes, AWS, GCP, Azure.
allowed-tools: Read, Grep, Glob, Bash(docker:*), Bash(docker-compose:*), Bash(kubectl:*), Bash(git:*)
---

# Deployment Expert Skill

Expert in application deployment, containerization, and infrastructure management.

## Expertise Areas

- **Containerization**: Docker, docker-compose, multi-stage builds
- **Orchestration**: Kubernetes, Docker Swarm, ECS
- **CI/CD**: GitHub Actions, GitLab CI, Jenkins, CircleCI
- **Cloud Platforms**: AWS, GCP, Azure deployment patterns
- **Infrastructure as Code**: Terraform, CloudFormation, Pulumi
- **Deployment Strategies**: Blue-green, canary, rolling updates

## Docker Best Practices

### Multi-Stage Dockerfile

```dockerfile
# Build stage
FROM python:3.11-slim as builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Runtime stage
FROM python:3.11-slim

# Create non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Copy only necessary files from builder
COPY --from=builder /root/.local /home/appuser/.local
COPY --chown=appuser:appuser . .

# Set PATH and user
ENV PATH=/home/appuser/.local/bin:$PATH
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "app.py"]
```

### docker-compose.yml Pattern

```yaml
version: '3.8'

services:
  app:
    build:
      context: .
      target: runtime
    image: myapp:${VERSION:-latest}
    container_name: myapp
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=redis://redis:6379
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
    networks:
      - app-network
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 3s
      retries: 3

  db:
    image: postgres:15-alpine
    container_name: myapp-db
    restart: unless-stopped
    environment:
      - POSTGRES_DB=${DB_NAME:-myapp}
      - POSTGRES_USER=${DB_USER:-postgres}
      - POSTGRES_PASSWORD=${DB_PASSWORD}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    networks:
      - app-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: myapp-redis
    restart: unless-stopped
    networks:
      - app-network

networks:
  app-network:
    driver: bridge

volumes:
  postgres-data:
```

## Kubernetes Deployment Patterns

### Deployment with ConfigMap and Secret

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  LOG_LEVEL: "info"
  ENVIRONMENT: "production"

---
apiVersion: v1
kind: Secret
metadata:
  name: app-secrets
type: Opaque
stringData:
  DATABASE_URL: "postgresql://user:pass@host:5432/db"
  API_KEY: "secret-api-key"

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
  labels:
    app: myapp
spec:
  replicas: 3
  selector:
    matchLabels:
      app: myapp
  template:
    metadata:
      labels:
        app: myapp
    spec:
      containers:
      - name: myapp
        image: myapp:1.0.0
        ports:
        - containerPort: 8000
        envFrom:
        - configMapRef:
            name: app-config
        - secretRef:
            name: app-secrets
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5

---
apiVersion: v1
kind: Service
metadata:
  name: myapp-service
spec:
  selector:
    app: myapp
  ports:
  - protocol: TCP
    port: 80
    targetPort: 8000
  type: LoadBalancer
```

## CI/CD Pipeline Examples

### GitHub Actions Workflow

```yaml
name: Deploy

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-cov

      - name: Run tests
        run: pytest --cov=. --cov-report=xml

      - name: Upload coverage
        uses: codecov/codecov-action@v3

  build:
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v2

      - name: Login to Docker Hub
        uses: docker/login-action@v2
        with:
          username: ${{ secrets.DOCKER_USERNAME }}
          password: ${{ secrets.DOCKER_PASSWORD }}

      - name: Build and push
        uses: docker/build-push-action@v4
        with:
          context: .
          push: true
          tags: |
            myapp:latest
            myapp:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy:
    needs: build
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - name: Deploy to production
        run: |
          # Update k8s deployment
          kubectl set image deployment/myapp \
            myapp=myapp:${{ github.sha }} \
            --record

          # Wait for rollout
          kubectl rollout status deployment/myapp
```

## Deployment Strategies

### Blue-Green Deployment

```bash
# Deploy new version (green)
kubectl apply -f deployment-v2.yaml

# Wait for ready
kubectl wait --for=condition=available --timeout=300s deployment/myapp-v2

# Switch traffic
kubectl patch service myapp -p '{"spec":{"selector":{"version":"v2"}}}'

# Monitor and rollback if needed
# If issues: kubectl patch service myapp -p '{"spec":{"selector":{"version":"v1"}}}'
```

### Canary Deployment

```yaml
# 90% old version
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp-stable
spec:
  replicas: 9
  # ... stable version config

---
# 10% new version
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp-canary
spec:
  replicas: 1
  # ... new version config
```

## Deployment Checklist

**Pre-deployment:**
- [ ] All tests passing
- [ ] Code reviewed and approved
- [ ] Version tagged in git
- [ ] Database migrations prepared
- [ ] Environment variables updated
- [ ] Secrets rotated if needed
- [ ] Backup taken
- [ ] Rollback plan documented

**During deployment:**
- [ ] Deploy to staging first
- [ ] Run smoke tests
- [ ] Check logs for errors
- [ ] Monitor resource usage
- [ ] Verify health checks

**Post-deployment:**
- [ ] Run integration tests
- [ ] Check metrics/dashboards
- [ ] Monitor error rates
- [ ] Verify all features working
- [ ] Document deployment

## Rollback Procedures

**Docker Compose:**
```bash
# Deploy previous version
docker-compose pull
docker-compose up -d --force-recreate

# Verify
docker-compose ps
docker-compose logs --tail=100
```

**Kubernetes:**
```bash
# Rollback to previous revision
kubectl rollout undo deployment/myapp

# Rollback to specific revision
kubectl rollout history deployment/myapp
kubectl rollout undo deployment/myapp --to-revision=2

# Monitor rollback
kubectl rollout status deployment/myapp
```

## Environment Management

**Development → Staging → Production pipeline:**

```bash
# .env.development
DATABASE_URL=postgresql://localhost/myapp_dev
DEBUG=true

# .env.staging
DATABASE_URL=${STAGING_DATABASE_URL}
DEBUG=true

# .env.production
DATABASE_URL=${PRODUCTION_DATABASE_URL}
DEBUG=false
```

## Monitoring & Health Checks

**Health endpoint example:**
```python
@app.get("/health")
def health_check():
    # Check dependencies
    db_healthy = check_database()
    redis_healthy = check_redis()

    if db_healthy and redis_healthy:
        return {"status": "healthy"}
    else:
        return {"status": "unhealthy", "details": {
            "database": "healthy" if db_healthy else "unhealthy",
            "redis": "healthy" if redis_healthy else "unhealthy"
        }}, 503
```

Use this skill for deployment planning, containerization, CI/CD setup, and infrastructure management.
