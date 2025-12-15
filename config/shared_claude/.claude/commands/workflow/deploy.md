---
description: Deploy to staging or production environment
allowed-tools: Bash(docker:*), Bash(docker-compose:*), Bash(git:*), Bash(kubectl:*), Bash(aws:*), Bash(gcloud:*)
argument-hint: [environment] [options]
---

# Deploy Application

## Current Branch and Status

!`git branch --show-current`
!`git status --short`

## Recent Commits

!`git log --oneline -5`

## Docker Images

!`docker images | grep -E "(clauditoreum|orchestrator)" | head -10`

## Task

Deploy to environment: $ARGUMENTS

**Pre-deployment Checklist:**
1. Verify git branch (main/master for production, develop for staging)
2. Ensure all tests pass
3. Check for uncommitted changes
4. Verify build artifacts exist
5. Review recent commits

**Deployment Instructions:**

**For Docker Compose deployments:**
```bash
# Build images
docker-compose build

# Deploy
docker-compose up -d

# Verify
docker-compose ps
docker-compose logs --tail=50
```

**For Kubernetes deployments:**
```bash
# Apply manifests
kubectl apply -f k8s/

# Check rollout
kubectl rollout status deployment/<name>

# Verify pods
kubectl get pods
```

**For Cloud deployments:**
- Check cloud provider CLI (aws, gcloud, azure)
- Review deployment configuration
- Apply changes with proper tags/versions

**Post-deployment:**
1. Verify services are running
2. Run smoke tests
3. Check logs for errors
4. Monitor resource usage
5. Rollback plan if issues detected

**Environment options:**
- `staging`: Deploy to staging environment
- `production` or `prod`: Deploy to production (requires confirmation)
- `local`: Local Docker deployment for testing

IMPORTANT: For production deployments, request explicit user confirmation before proceeding.
