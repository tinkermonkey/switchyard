#!/bin/bash
# scripts/fix-snap-mounts.sh
# Fix snap docker mount issues by replacing stale directories with real files
#
# REQUIRES: sudo access (stale directories are owned by root)
#
# Usage:
#   sudo bash scripts/fix-snap-mounts.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run with sudo"
    echo "Usage: sudo bash scripts/fix-snap-mounts.sh"
    exit 1
fi

# Get the actual user (not root when using sudo)
ACTUAL_USER=${SUDO_USER:-$USER}
ACTUAL_HOME=$(eval echo ~$ACTUAL_USER)

echo "=== Snap Docker Mount Fix ==="
echo ""
echo "User: $ACTUAL_USER"
echo "Home: $ACTUAL_HOME"
echo ""

# Find snap revision
if [ -L "$ACTUAL_HOME/snap/docker/current" ]; then
    SNAP_REV=$(readlink -f "$ACTUAL_HOME/snap/docker/current" | xargs basename)
else
    SNAP_REV=$(ls -d "$ACTUAL_HOME/snap/docker"/[0-9]* 2>/dev/null | xargs basename | tail -1)
fi

if [ -z "$SNAP_REV" ]; then
    echo "Error: Could not find snap docker revision"
    exit 1
fi

SNAP_PATH="$ACTUAL_HOME/snap/docker/$SNAP_REV"
echo "Snap revision: $SNAP_REV"
echo "Snap path: $SNAP_PATH"
echo ""

# Ask for confirmation
read -p "This will replace snap docker mount paths. Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Stopping orchestrator..."
cd "$ACTUAL_HOME/workspace/orchestrator/clauditoreum"
runuser -u $ACTUAL_USER -- docker-compose down

echo ""
echo "Fixing mount paths..."

# Fix .gitconfig
echo ""
echo "[1/3] Fixing .gitconfig..."
if [ -f "$ACTUAL_HOME/.gitconfig" ]; then
    echo "  Removing stale directory: $SNAP_PATH/.gitconfig"
    rm -rf "$SNAP_PATH/.gitconfig"

    echo "  Copying real file: $ACTUAL_HOME/.gitconfig"
    cp "$ACTUAL_HOME/.gitconfig" "$SNAP_PATH/.gitconfig"
    chown $ACTUAL_USER:$ACTUAL_USER "$SNAP_PATH/.gitconfig"
    chmod 644 "$SNAP_PATH/.gitconfig"

    echo "  ✓ .gitconfig fixed ($(stat -c%s "$SNAP_PATH/.gitconfig") bytes)"
else
    echo "  Warning: Real .gitconfig not found at $ACTUAL_HOME/.gitconfig"
fi

# Fix .ssh/id_ed25519
echo ""
echo "[2/3] Fixing .ssh/id_ed25519..."
if [ -f "$ACTUAL_HOME/.ssh/id_ed25519" ]; then
    echo "  Removing stale directory: $SNAP_PATH/.ssh"
    rm -rf "$SNAP_PATH/.ssh"

    echo "  Creating .ssh directory"
    mkdir -p "$SNAP_PATH/.ssh"

    echo "  Copying SSH key: $ACTUAL_HOME/.ssh/id_ed25519"
    cp "$ACTUAL_HOME/.ssh/id_ed25519" "$SNAP_PATH/.ssh/"

    # Copy public key if it exists
    if [ -f "$ACTUAL_HOME/.ssh/id_ed25519.pub" ]; then
        cp "$ACTUAL_HOME/.ssh/id_ed25519.pub" "$SNAP_PATH/.ssh/"
    fi

    # Set proper ownership and permissions
    chown -R $ACTUAL_USER:$ACTUAL_USER "$SNAP_PATH/.ssh"
    chmod 700 "$SNAP_PATH/.ssh"
    chmod 600 "$SNAP_PATH/.ssh/id_ed25519"
    [ -f "$SNAP_PATH/.ssh/id_ed25519.pub" ] && chmod 644 "$SNAP_PATH/.ssh/id_ed25519.pub"

    echo "  ✓ SSH key fixed ($(stat -c%s "$SNAP_PATH/.ssh/id_ed25519") bytes)"
else
    echo "  Warning: Real SSH key not found at $ACTUAL_HOME/.ssh/id_ed25519"
fi

# Fix .orchestrator
echo ""
echo "[3/3] Fixing .orchestrator..."
if [ -d "$ACTUAL_HOME/.orchestrator" ]; then
    echo "  Removing stale directory: $SNAP_PATH/.orchestrator"
    rm -rf "$SNAP_PATH/.orchestrator"

    echo "  Creating directory and copying contents"
    mkdir -p "$SNAP_PATH/.orchestrator"
    if [ "$(ls -A "$ACTUAL_HOME/.orchestrator")" ]; then
        cp -r "$ACTUAL_HOME/.orchestrator"/* "$SNAP_PATH/.orchestrator/"
        echo "  Copied $(ls "$ACTUAL_HOME/.orchestrator" | wc -l) files"
    else
        echo "  Warning: .orchestrator directory is empty"
    fi

    chown -R $ACTUAL_USER:$ACTUAL_USER "$SNAP_PATH/.orchestrator"
    chmod 755 "$SNAP_PATH/.orchestrator"

    echo "  ✓ .orchestrator fixed"
else
    echo "  Warning: Real .orchestrator not found at $ACTUAL_HOME/.orchestrator"
fi

echo ""
echo "Restarting orchestrator..."
runuser -u $ACTUAL_USER -- docker-compose up -d orchestrator

echo ""
echo "Waiting for container to start..."
sleep 5

echo ""
echo "=== Verification ==="
echo ""

# Verify mounts in container
CONTAINER=$(runuser -u $ACTUAL_USER -- docker ps --format '{{.Names}}' | grep orchestrator | head -1)
if [ -n "$CONTAINER" ]; then
    echo "Testing mounts in container: $CONTAINER"
    echo ""

    runuser -u $ACTUAL_USER -- docker exec $CONTAINER bash -c '
        success=0
        total=0

        for mount in .gitconfig .ssh/id_ed25519; do
            path="/home/orchestrator/$mount"
            total=$((total + 1))

            if [ -f "$path" ]; then
                size=$(stat -c%s "$path" 2>/dev/null || echo 0)
                echo "  ✓ $mount is a FILE ($size bytes)"
                success=$((success + 1))
            elif [ -d "$path" ]; then
                echo "  ✗ $mount is still a DIRECTORY (fix failed)"
            else
                echo "  ? $mount does not exist"
            fi
        done

        # .orchestrator should be a directory
        if [ -d "/home/orchestrator/.orchestrator" ]; then
            echo "  ✓ .orchestrator is a DIRECTORY"
            success=$((success + 1))
            total=$((total + 1))
        fi

        echo ""
        echo "Result: $success/$total mounts fixed"

        if [ $success -eq $total ]; then
            exit 0
        else
            exit 1
        fi
    '

    if [ $? -eq 0 ]; then
        echo ""
        echo "=== SUCCESS ==="
        echo "All mounts are now working correctly!"
        echo ""
        echo "Test git operations:"
        echo "  docker-compose exec orchestrator git config user.name"
        echo "  docker-compose exec orchestrator git config user.email"
    else
        echo ""
        echo "=== PARTIAL SUCCESS ==="
        echo "Some mounts may still have issues. Check the output above."
    fi
else
    echo "Warning: Container not running, cannot verify mounts"
fi

echo ""
echo "Note: If snap docker upgrades to a new revision, you'll need to run this script again."
echo "Consider creating a wrapper script that runs this before 'docker-compose up'."
