#!/bin/bash
# Migration script: Organization-level to Repository-level GitHub Projects
# This script backs up existing state and clears it to allow the orchestrator
# to create fresh repository-level projects instead of organization-level projects.

set -e  # Exit on any error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
STATE_DIR="$PROJECT_ROOT/state/projects"

echo "=================================================="
echo "GitHub Projects Migration Script"
echo "Organization-level → Repository-level"
echo "=================================================="
echo ""

# Check if state directory exists
if [ ! -d "$STATE_DIR" ]; then
    echo "❌ State directory not found: $STATE_DIR"
    exit 1
fi

# Create backup with timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$PROJECT_ROOT/state/projects.backup.org-level.$TIMESTAMP"

echo "📦 Step 1: Creating backup of current state..."
echo "   Backup location: $BACKUP_DIR"

cp -r "$STATE_DIR" "$BACKUP_DIR"

if [ -d "$BACKUP_DIR" ]; then
    echo "   ✅ Backup created successfully"
else
    echo "   ❌ Backup failed"
    exit 1
fi

echo ""
echo "📋 Step 2: Analyzing current state..."

# Count projects with state
project_count=0
projects_to_clear=()

for project_dir in "$STATE_DIR"/*/ ; do
    if [ -d "$project_dir" ]; then
        project_name=$(basename "$project_dir")
        
        # Skip backup directories
        if [[ "$project_name" == *.backup.* ]]; then
            continue
        fi
        
        github_state_file="$project_dir/github_state.yaml"
        
        if [ -f "$github_state_file" ]; then
            project_count=$((project_count + 1))
            projects_to_clear+=("$project_name")
            
            echo "   📂 $project_name"
            
            # Extract and show project numbers
            if grep -q "project_number:" "$github_state_file"; then
                echo "      Organization-level boards found"
                grep "project_number:" "$github_state_file" | head -3 | while read -r line; do
                    echo "         $line"
                done
            fi
        fi
    fi
done

if [ $project_count -eq 0 ]; then
    echo "   ℹ️  No project state files found. Nothing to migrate."
    exit 0
fi

echo ""
echo "   Found $project_count project(s) with state files"

echo ""
echo "🗑️  Step 3: Clearing GitHub state files..."

for project_name in "${projects_to_clear[@]}"; do
    github_state_file="$STATE_DIR/$project_name/github_state.yaml"
    
    if [ -f "$github_state_file" ]; then
        rm "$github_state_file"
        echo "   ✅ Cleared: $project_name/github_state.yaml"
    fi
done

echo ""
echo "=================================================="
echo "✅ Migration preparation complete!"
echo "=================================================="
echo ""
echo "📝 What happened:"
echo "   1. Backup created at: $BACKUP_DIR"
echo "   2. Cleared $project_count project state file(s)"
echo ""
echo "🔄 Next steps:"
echo "   1. Restart the orchestrator:"
echo "      docker compose restart orchestrator"
echo ""
echo "   2. Monitor the logs to see new repo-level boards being created:"
echo "      docker compose logs -f orchestrator"
echo ""
echo "   3. The orchestrator will automatically create NEW repository-level boards"
echo ""
echo "⚠️  Important notes:"
echo "   • Old organization-level boards will NOT be deleted automatically"
echo "   • You can manually delete them from GitHub's Projects UI if desired"
echo "   • Old board numbers shown above for reference"
echo "   • To rollback: Copy files from backup directory back to state/projects/"
echo ""
echo "📚 Rollback command (if needed):"
echo "   cp -r $BACKUP_DIR/* $STATE_DIR/"
echo ""
