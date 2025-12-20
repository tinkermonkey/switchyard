# Medic Data Migration Guide

## Overview

This guide covers the migration of existing Docker failure data from the old schema to the new unified medic architecture introduced in v2.0.0.

**Migration scope:**
- **Elasticsearch**: `medic-failure-signatures-*` → `medic-docker-failures-*`
- **Redis**: `medic:investigation:*` → `medic:docker_investigation:*`

**Schema changes:**
- Add `type: "docker"` field (distinguishes from Claude failures)
- Add `project: "orchestrator"` field (enables multi-project support)
- Rename `sample_log_entries` → `sample_entries` (unified field name)
- Add `total_failures` field (consistent with Claude system)

---

## Prerequisites

1. **Backup current data** (recommended):
   ```bash
   # Elasticsearch snapshot
   curl -X PUT "localhost:9200/_snapshot/medic_backup/migration_$(date +%Y%m%d)" -H 'Content-Type: application/json' -d '{
     "indices": "medic-failure-signatures-*",
     "ignore_unavailable": true,
     "include_global_state": false
   }'

   # Redis backup
   redis-cli --rdb /backup/medic-redis-$(date +%Y%m%d).rdb
   ```

2. **Check current data counts**:
   ```bash
   # Elasticsearch
   curl "localhost:9200/medic-failure-signatures-*/_count"

   # Redis
   redis-cli KEYS "medic:investigation:*" | wc -l
   ```

3. **Ensure services are running**:
   ```bash
   docker-compose ps elasticsearch redis
   ```

---

## Migration Steps

### Step 1: Elasticsearch Data Migration

#### 1.1 Dry Run (Preview)

Preview what will be migrated without making changes:

```bash
python scripts/migrate_docker_failures.py --dry-run
```

**Expected output:**
```
================================================================================
DRY RUN: Preview Migration
================================================================================
  medic-failure-signatures-2025.01.15 (150 docs)
    → medic-docker-failures-2025.01.15
================================================================================
Total documents to migrate: 150
Total indices to create: 1
================================================================================

Sample document transformation:
OLD SCHEMA:
{
  "fingerprint_id": "abc123...",
  "signature": {...},
  "occurrence_count": 5,
  "sample_log_entries": [...]
}

NEW SCHEMA:
{
  "type": "docker",
  "project": "orchestrator",
  "fingerprint_id": "abc123...",
  "signature": {...},
  "occurrence_count": 5,
  "total_failures": 5,
  "sample_entries": [...]
}
```

#### 1.2 Execute Migration

Run the actual migration:

```bash
python scripts/migrate_docker_failures.py --execute
```

**Prompts:**
```
⚠️  About to migrate 150 documents
This will create new indices with transformed data.
Old indices will be preserved for backup.

Continue? [y/N]: y
```

**Process:**
1. Creates new indices with same settings/mappings
2. Transforms and copies documents in batches (500 at a time)
3. Preserves old indices for backup
4. Reports success/error counts

**Expected output:**
```
================================================================================
EXECUTING MIGRATION
================================================================================

Migrating: medic-failure-signatures-2025.01.15 → medic-docker-failures-2025.01.15
  Created index: medic-docker-failures-2025.01.15
  Migrated 150 documents
================================================================================
Migration complete: 150 documents migrated, 0 errors
================================================================================
```

#### 1.3 Verify Migration

Verify the migration completed successfully:

```bash
python scripts/migrate_docker_failures.py --verify
```

**Checks performed:**
- ✅ Document counts match (old vs new)
- ✅ Required fields present (`type`, `project`, `sample_entries`, `total_failures`)
- ✅ Field values correct

**Expected output:**
```
================================================================================
VERIFYING MIGRATION
================================================================================
Old indices (medic-failure-signatures-*): 150 documents
New indices (medic-docker-failures-*): 150 documents
✅ Document counts match!

Sample document from new index:
✅ All required fields present
  type: docker
  project: orchestrator
  sample_entries: 10 entries
  total_failures: 5
================================================================================

✅ Migration verification passed!

Old indices are preserved for 30 days as backup.
To delete old indices after verification:
  # Wait 30 days, then run:
  # curl -X DELETE 'http://localhost:9200/medic-failure-signatures-*'
```

---

### Step 2: Redis Key Migration

#### 2.1 Dry Run (Preview)

Preview Redis key changes:

```bash
python scripts/migrate_redis_keys.py --dry-run
```

**Expected output:**
```
================================================================================
DRY RUN: Preview Redis Key Migration
================================================================================
  medic:investigation:queue (list)
    → medic:docker_investigation:queue
  medic:investigation:active (set)
    → medic:docker_investigation:active
  medic:investigation:status:abc123 (string)
    → medic:docker_investigation:status:abc123
================================================================================
Total keys to migrate: 15
================================================================================

Key type breakdown:
  list: 1
  set: 1
  string: 13
```

#### 2.2 Execute Migration

Run the actual migration:

**Option A: Preserve old keys (recommended)**
```bash
python scripts/migrate_redis_keys.py --execute
```

**Option B: Delete old keys after migration**
```bash
python scripts/migrate_redis_keys.py --execute --delete-old-keys
```

**Prompts:**
```
⚠️  About to migrate 15 Redis keys
Old keys will be preserved for backup

Continue? [y/N]: y
```

**Expected output:**
```
================================================================================
EXECUTING REDIS KEY MIGRATION
================================================================================
Migrating: medic:investigation:queue → medic:docker_investigation:queue
Migrating: medic:investigation:active → medic:docker_investigation:active
Migrating: medic:investigation:status:abc123 → medic:docker_investigation:status:abc123
================================================================================
Migration complete: 15 keys migrated, 0 errors
Old keys preserved (not deleted)
================================================================================
```

#### 2.3 Verify Migration

Verify Redis migration:

```bash
python scripts/migrate_redis_keys.py --verify
```

**Expected output:**
```
================================================================================
VERIFYING REDIS KEY MIGRATION
================================================================================
Old keys (medic:investigation:*): 15
New keys (medic:docker_investigation:*): 15
✅ Key counts match!

Verifying key types:
  medic:docker_investigation:queue: list, 5 items
  medic:docker_investigation:active: set, 2 members
  Status keys: 10
    Sample: medic:docker_investigation:status:abc123 = in_progress
================================================================================

✅ Migration verification passed!

Old keys are preserved unless --delete-old-keys was used.
```

---

## Step 3: Restart Services

After migration, restart the medic service to use the new indices:

```bash
docker-compose restart orchestrator
```

**Monitor logs:**
```bash
docker-compose logs -f orchestrator | grep -i medic
```

**Expected log messages:**
```
[INFO] DockerFailureSignatureStore initialized with index: medic-docker-failures-*
[INFO] DockerInvestigationQueue initialized with prefix: medic:docker_investigation
```

---

## Verification Checklist

After migration and service restart:

- [ ] Elasticsearch migration verified (document counts match)
- [ ] Redis migration verified (key counts match)
- [ ] Services restarted successfully
- [ ] New failures are written to `medic-docker-failures-*` indices
- [ ] Investigation queue uses `medic:docker_investigation:*` keys
- [ ] Web UI displays failure signatures correctly
- [ ] API endpoints return data from new indices

**Manual verification:**

```bash
# Check new Elasticsearch index
curl -s "localhost:9200/medic-docker-failures-*/_search?size=1" | jq '.hits.hits[]._source | {type, project, fingerprint_id}'

# Expected output:
{
  "type": "docker",
  "project": "orchestrator",
  "fingerprint_id": "abc123..."
}

# Check new Redis keys
redis-cli KEYS "medic:docker_investigation:*"

# Expected output:
medic:docker_investigation:queue
medic:docker_investigation:active
medic:docker_investigation:status:abc123
...
```

---

## Rollback Procedure

If issues occur after migration:

### Rollback Option 1: Revert to old indices/keys

```bash
# Stop services
docker-compose stop orchestrator

# Delete new indices
curl -X DELETE "localhost:9200/medic-docker-failures-*"

# Delete new Redis keys
redis-cli --scan --pattern "medic:docker_investigation:*" | xargs redis-cli DEL

# Revert code to previous version
git checkout <previous-commit>

# Restart services
docker-compose up -d orchestrator
```

### Rollback Option 2: Run both old and new systems in parallel

Temporarily support both schemas by modifying services to read from both index patterns:

```python
# In code: Query both patterns
indices = "medic-failure-signatures-*,medic-docker-failures-*"
```

---

## Cleanup (After 30 Days)

Once you've verified the migration is successful and the system is stable:

### Delete old Elasticsearch indices

```bash
# Verify new system is working
python scripts/migrate_docker_failures.py --verify

# Delete old indices
curl -X DELETE "localhost:9200/medic-failure-signatures-*"
```

### Delete old Redis keys (if not already deleted)

```bash
# List old keys
redis-cli KEYS "medic:investigation:*"

# Delete old keys
redis-cli --scan --pattern "medic:investigation:*" | xargs redis-cli DEL
```

---

## Troubleshooting

### Issue: Document count mismatch

**Symptoms:**
```
⚠️  Document count mismatch: 150 vs 145
```

**Solutions:**
1. Check for errors during migration:
   ```bash
   python scripts/migrate_docker_failures.py --execute 2>&1 | grep -i error
   ```

2. Re-run migration (only migrates missing documents):
   ```bash
   python scripts/migrate_docker_failures.py --execute
   ```

### Issue: Redis keys already exist

**Symptoms:**
```
Key medic:docker_investigation:queue already exists, skipping...
```

**Solutions:**
1. If migration was interrupted, delete new keys and re-run:
   ```bash
   redis-cli --scan --pattern "medic:docker_investigation:*" | xargs redis-cli DEL
   python scripts/migrate_redis_keys.py --execute
   ```

### Issue: Services not using new indices

**Symptoms:**
- New failures not appearing in `medic-docker-failures-*`
- Logs show old index pattern

**Solutions:**
1. Verify code is updated:
   ```bash
   git status
   git log -1 --oneline
   ```

2. Force rebuild and restart:
   ```bash
   docker-compose build orchestrator
   docker-compose restart orchestrator
   ```

### Issue: Permission denied on Elasticsearch

**Symptoms:**
```
Failed to create index: security_exception
```

**Solutions:**
1. Check Elasticsearch authentication is disabled or credentials are correct
2. Verify user has create_index permission

---

## Support

For migration issues:
1. Check logs: `docker-compose logs orchestrator medic`
2. Review this guide's Troubleshooting section
3. Create GitHub issue with migration logs

---

## Summary

Migration timeline:
- **Step 1**: Elasticsearch migration (~5-10 minutes for 1000 documents)
- **Step 2**: Redis migration (~1 minute)
- **Step 3**: Service restart (~30 seconds)
- **Total**: ~15 minutes

**Data safety:**
- Old indices/keys preserved by default
- Can rollback at any time within 30 days
- No data loss if migration fails (atomic operations)

**Post-migration:**
- Both Docker and Claude failure tracking use unified base classes
- Reduced code duplication (~2,800 lines eliminated)
- Easier to maintain and extend
