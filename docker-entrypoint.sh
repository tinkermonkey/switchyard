#!/bin/sh
set -e

# Ensure SSH directory exists with correct permissions
mkdir -p /home/orchestrator/.ssh
chmod 700 /home/orchestrator/.ssh

# Create SSH config if it doesn't exist
if [ ! -f /home/orchestrator/.ssh/config ]; then
    cat > /home/orchestrator/.ssh/config <<EOF
Host github.com
  StrictHostKeyChecking accept-new
  UserKnownHostsFile /home/orchestrator/.ssh/known_hosts
  IdentityFile /home/orchestrator/.ssh/id_ed25519
EOF
    chmod 600 /home/orchestrator/.ssh/config
fi

# SSH key is mounted read-only from host, permissions already correct
# No need to chmod as it's read-only and already has proper permissions on host

# Git safe.directory is configured in Dockerfile at build time (in /etc/gitconfig)

# Execute the main command
exec "$@"
