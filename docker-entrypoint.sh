#!/bin/sh
set -e

# Fix mount issues where .gitconfig or .orchestrator were created as directories
# This can happen if Docker creates mount points before volumes are mounted
if [ -d /home/orchestrator/.gitconfig ] && [ ! -f /home/orchestrator/.gitconfig ]; then
    echo "Warning: .gitconfig is a directory, removing to allow file mount"
    rm -rf /home/orchestrator/.gitconfig 2>/dev/null || true
fi

if [ -d /home/orchestrator/.orchestrator ] && [ -z "$(ls -A /home/orchestrator/.orchestrator 2>/dev/null)" ]; then
    echo "Warning: .orchestrator is an empty directory, removing to allow proper mount"
    rm -rf /home/orchestrator/.orchestrator 2>/dev/null || true
fi

# SSH directory setup - handle both writable and read-only scenarios
# In orchestrator container, .ssh may be a mix of mounted (ro) and local files
# In agent containers, .ssh is entirely mounted read-only

# Try to ensure SSH directory exists - may fail if parent is read-only
mkdir -p /home/orchestrator/.ssh 2>/dev/null || true

# Try to set permissions - will fail if read-only, which is OK
chmod 700 /home/orchestrator/.ssh 2>/dev/null || true

# Create SSH config if it doesn't exist and we can write
# Use temp file approach to handle read-only filesystem gracefully
if [ ! -f /home/orchestrator/.ssh/config ]; then
    if touch /home/orchestrator/.ssh/.write-test 2>/dev/null; then
        rm -f /home/orchestrator/.ssh/.write-test
        cat > /home/orchestrator/.ssh/config <<EOF
Host github.com
  StrictHostKeyChecking accept-new
  UserKnownHostsFile /home/orchestrator/.ssh/known_hosts
  IdentityFile /home/orchestrator/.ssh/id_ed25519
EOF
        chmod 600 /home/orchestrator/.ssh/config 2>/dev/null || true
    fi
fi

# SSH key is mounted read-only from host, permissions already correct
# Git safe.directory is configured in Dockerfile at build time (in /etc/gitconfig)

# Execute the main command
exec "$@"

