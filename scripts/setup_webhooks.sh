#!/bin/bash

# Check if .env exists and has webhook secret
if [ -f .env ]; then
    source .env
fi

if [ -z "$GITHUB_WEBHOOK_SECRET" ]; then
    echo "🔐 Generating new webhook secret..."
    GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)
    echo "GITHUB_WEBHOOK_SECRET=$GITHUB_WEBHOOK_SECRET" >> .env
    echo "✅ Generated and saved webhook secret"
else
    echo "✅ Using existing webhook secret"
fi

# Get current ngrok URL (after ngrok is running)
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | jq -r '.tunnels[0].public_url')

if [ -z "$NGROK_URL" ]; then
    echo "❌ ngrok not running. Start it with: docker-compose up -d ngrok"
    exit 1
fi

echo "📡 Setting up webhooks with:"
echo "   URL: ${NGROK_URL}/github-webhook"
echo "   Secret: [hidden]"

# Get GitHub username/org from env
if [ -z "$GITHUB_ORG" ]; then
    echo "❌ GITHUB_ORG not set in .env file"
    exit 1
fi

# Auto-discover projects from config/projects.yaml or GitHub
PROJECTS_FILE="config/projects.yaml"
if [ -f "$PROJECTS_FILE" ]; then
    echo "📋 Reading projects from $PROJECTS_FILE..."
    PROJECTS=$(python3 -c "
import yaml
with open('$PROJECTS_FILE', 'r') as f:
    config = yaml.safe_load(f)
for name in config.get('projects', {}):
    print(name)
    ")
else
    echo "📋 Auto-discovering repositories from GitHub..."
    PROJECTS=$(gh repo list $GITHUB_ORG --limit 100 --json name --jq '.[].name')
fi

# Configure webhook for each project
for PROJECT in $PROJECTS; do
    echo "Setting up webhook for $GITHUB_ORG/$PROJECT..."

    gh api repos/$GITHUB_ORG/$PROJECT/hooks \
        --method POST \
        --field name='web' \
        --field active=true \
        --field events='["issues", "project_card", "pull_request", "pull_request_review"]' \
        --field config[url]="${NGROK_URL}/github-webhook" \
        --field config[content_type]='json' \
        --field config[secret]="$GITHUB_WEBHOOK_SECRET" || echo "⚠️ Failed to create webhook for $PROJECT (might already exist)"
done

echo "✅ Webhooks configured!"