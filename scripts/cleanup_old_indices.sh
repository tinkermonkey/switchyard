#!/bin/bash
# Clean up Elasticsearch indices older than N days
# Usage: ./cleanup_old_indices.sh [days] [dry-run]
# Example: ./cleanup_old_indices.sh 7 dry-run

set -e

ELASTICSEARCH_HOST="${ELASTICSEARCH_HOST:-localhost:9200}"
DAYS_TO_KEEP="${1:-7}"
DRY_RUN="${2:-}"

echo "Cleaning up indices older than ${DAYS_TO_KEEP} days..."
if [[ "$DRY_RUN" == "dry-run" ]]; then
    echo "DRY RUN MODE - No indices will be deleted"
fi

# Calculate cutoff date (DAYS_TO_KEEP days ago)
CUTOFF_DATE=$(date -d "${DAYS_TO_KEEP} days ago" +%Y-%m-%d)
echo "Cutoff date: ${CUTOFF_DATE}"

# Get all date-based indices
INDICES=$(curl -s "http://${ELASTICSEARCH_HOST}/_cat/indices/*-20??-??-???h=index" | grep -E '.*-[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)

if [[ -z "$INDICES" ]]; then
    echo "No date-based indices found"
    exit 0
fi

# Process each index
DELETED_COUNT=0
while IFS= read -r INDEX; do
    # Extract date from index name (assumes format: prefix-YYYY-MM-DD)
    if [[ $INDEX =~ ([0-9]{4}-[0-9]{2}-[0-9]{2}) ]]; then
        INDEX_DATE="${BASH_REMATCH[1]}"
        
        # Compare dates
        if [[ "$INDEX_DATE" < "$CUTOFF_DATE" ]]; then
            if [[ "$DRY_RUN" == "dry-run" ]]; then
                echo "[DRY RUN] Would delete: $INDEX (date: $INDEX_DATE)"
            else
                echo "Deleting: $INDEX (date: $INDEX_DATE)"
                curl -s -X DELETE "http://${ELASTICSEARCH_HOST}/${INDEX}" > /dev/null
                ((DELETED_COUNT++))
            fi
        else
            echo "Keeping: $INDEX (date: $INDEX_DATE)"
        fi
    fi
done <<< "$INDICES"

if [[ "$DRY_RUN" == "dry-run" ]]; then
    echo "DRY RUN complete - No indices were deleted"
else
    echo "Cleanup complete - Deleted ${DELETED_COUNT} indices"
fi
