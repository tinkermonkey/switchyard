#!/bin/sh
set -e

# Clean up empty .orchestrator directory if mount failed
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

if [ -f /home/orchestrator/.ssh/config ]; then
    # A config already exists — e.g. agent containers mount the host's whole
    # ~/.ssh directory (including its real config, correctly pointing
    # IdentityFile at the actual deploy key) read-only. OpenSSH refuses to
    # honor ~/.ssh/config or private keys that aren't strictly
    # owned/permissioned ("Bad owner or permissions"), and a read-only bind
    # mount means we cannot chmod/chown the mounted files in place to satisfy
    # that — so the mounted config can silently go unused. Stage a writable
    # copy with corrected permissions, rewrite its paths to point at the
    # staged copy, and point git/ssh at it explicitly instead of relying on
    # default ~/.ssh discovery honoring the raw mounted files.
    STAGED_SSH_DIR=/home/orchestrator/.ssh-runtime
    mkdir -p "$STAGED_SSH_DIR"
    chmod 700 "$STAGED_SSH_DIR"
    cp -p /home/orchestrator/.ssh/* "$STAGED_SSH_DIR"/ 2>/dev/null || true
    chmod 600 "$STAGED_SSH_DIR"/* 2>/dev/null || true
    chmod 700 "$STAGED_SSH_DIR"
    # Redirect any absolute or ~-relative references to the original
    # (unwritable, permission-mismatched) mount so IdentityFile/
    # UserKnownHostsFile resolve to the staged copy instead.
    sed -i "s#/home/orchestrator/\.ssh#$STAGED_SSH_DIR#g; s#~/\.ssh#$STAGED_SSH_DIR#g" \
        "$STAGED_SSH_DIR/config" 2>/dev/null || true
    export GIT_SSH_COMMAND="ssh -F $STAGED_SSH_DIR/config"
else
    # Create SSH config if it doesn't exist and we can write
    # Use temp file approach to handle read-only filesystem gracefully
    if touch /home/orchestrator/.ssh/.write-test 2>/dev/null; then
        rm -f /home/orchestrator/.ssh/.write-test
        cat > /home/orchestrator/.ssh/config <<EOF
Host github.com
  StrictHostKeyChecking accept-new
  UserKnownHostsFile /home/orchestrator/.ssh/known_hosts
  IdentityFile /home/orchestrator/.ssh/id_github
EOF
        chmod 600 /home/orchestrator/.ssh/config 2>/dev/null || true
    fi
fi

# SSH key is mounted read-only from host, permissions already correct
# Git safe.directory is configured in Dockerfile at build time (in /etc/gitconfig)

# Authenticate GitHub CLI if GITHUB_TOKEN is set
if [ -n "$GITHUB_TOKEN" ]; then
    # Ensure .config directory exists for gh to save config
    mkdir -p /home/orchestrator/.config
    chmod 700 /home/orchestrator/.config 2>/dev/null || true

    # Authenticate gh CLI with token
    # Suppress all output (both stdout and stderr) since gh may fail if config is read-only
    # The token will still be available as GITHUB_TOKEN environment variable for API calls
    echo "$GITHUB_TOKEN" | gh auth login --with-token >/dev/null 2>&1 || true
fi

# Execute the main command
exec "$@"

