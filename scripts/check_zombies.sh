#!/bin/bash
# Check for zombie processes in the orchestrator container
# This script can be run periodically to monitor for zombie accumulation

echo "=== Zombie Process Check ==="
echo "Timestamp: $(date)"
echo ""

# Get container name
CONTAINER="clauditoreum-orchestrator-1"

# Count zombie processes
ZOMBIE_COUNT=$(docker exec $CONTAINER ps -eo stat | grep -c '^Z')

echo "Total zombie processes: $ZOMBIE_COUNT"
echo ""

if [ $ZOMBIE_COUNT -gt 0 ]; then
    echo "=== Zombie Process Details ==="
    docker exec $CONTAINER ps -eo stat,ppid,pid,cmd | grep "^Z" | head -20
    echo ""
    
    if [ $ZOMBIE_COUNT -gt 20 ]; then
        echo "WARNING: More than 20 zombie processes detected!"
        echo "This indicates the zombie process reaper may not be working correctly."
    elif [ $ZOMBIE_COUNT -gt 10 ]; then
        echo "NOTICE: More than 10 zombie processes - monitor for accumulation"
    fi
else
    echo "✓ No zombie processes detected - system healthy"
fi

echo ""
echo "=== Active Threads ==="
docker exec $CONTAINER python -c "import threading; print('Thread count:', threading.active_count()); [print(f'  {t.name} (daemon={t.daemon})') for t in threading.enumerate()]"

echo ""
echo "=== Main Process Info ==="
docker exec $CONTAINER ps -p 1 -o pid,ppid,stat,%cpu,%mem,etime,cmd
