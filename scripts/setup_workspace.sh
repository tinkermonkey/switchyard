#!/bin/bash

# Setup workspace using the new Docker-based project management
echo "🚀 Setting up SDLC workspace..."

# Check if we're in the orchestrator directory
if [ ! -f "docker-compose.yml" ]; then
    echo "❌ Must run from orchestrator directory (contains docker-compose.yml)"
    exit 1
fi

# Check .env file exists
if [ ! -f ".env" ]; then
    echo "❌ .env file not found. Copy .env.example to .env and configure it first."
    exit 1
fi

# Source environment variables
source .env

# Create projects directory if it doesn't exist
mkdir -p ./projects

echo "📋 Setting up project configuration..."

# Run the project setup script to auto-discover and configure projects
python3 scripts/setup_projects.py

echo "🏗️ Setting up Docker environment..."

# Build and start the orchestrator services
docker-compose build
docker-compose up -d redis ngrok webhook

# Wait for services to be ready
echo "⏳ Waiting for services to start..."
sleep 5

# Check if ngrok is running and get URL
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | jq -r '.tunnels[0].public_url' 2>/dev/null)

if [ "$NGROK_URL" != "null" ] && [ -n "$NGROK_URL" ]; then
    echo "📡 ngrok tunnel active: $NGROK_URL"

    # Update .env with ngrok URL
    if grep -q "NGROK_URL=" .env; then
        sed -i "s|NGROK_URL=.*|NGROK_URL=$NGROK_URL|" .env
    else
        echo "NGROK_URL=$NGROK_URL" >> .env
    fi

    # Offer to set up webhooks
    read -p "🔗 Set up GitHub webhooks? (y/N): " setup_webhooks
    if [ "$setup_webhooks" = "y" ] || [ "$setup_webhooks" = "Y" ]; then
        ./scripts/setup_webhooks.sh
    fi
else
    echo "⚠️ ngrok not ready yet. You can set up webhooks later with: ./scripts/setup_webhooks.sh"
fi

# Clone/update all configured projects using Docker orchestrator
echo "📦 Setting up project repositories..."
docker-compose run --rm orchestrator python3 -c "
from services.project_manager import ProjectManager
import yaml

pm = ProjectManager()

try:
    with open('config/projects.yaml', 'r') as f:
        config = yaml.safe_load(f)

    for project_name in config.get('projects', {}):
        try:
            print(f'Setting up {project_name}...')
            pm.ensure_project_cloned(project_name)
        except Exception as e:
            print(f'⚠️ Failed to setup {project_name}: {e}')
except FileNotFoundError:
    print('📋 No projects.yaml found - run scripts/setup_projects.py first')
"

echo ""
echo "🎉 Workspace setup complete!"
echo ""
echo "📝 Next steps:"
echo "   1. Review config/projects.yaml and customize tech_stacks"
echo "   2. Start the orchestrator: docker-compose up orchestrator"
echo "   3. Projects are available in: ./projects/"
echo "   4. Monitor with: docker-compose logs -f"