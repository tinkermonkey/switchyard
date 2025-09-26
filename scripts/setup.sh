#!/bin/bash
set -e

echo "🚀 Claude Code Orchestrator Setup"
echo "=================================="

# Check prerequisites
echo "🔍 Checking prerequisites..."

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found. Please install Docker Desktop."
    exit 1
fi

# Check Docker Compose
if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose not found. Please install Docker Compose."
    exit 1
fi

# Check GitHub CLI
if ! command -v gh &> /dev/null; then
    echo "❌ GitHub CLI not found. Please install: brew install gh"
    exit 1
fi

# Check git
if ! command -v git &> /dev/null; then
    echo "❌ git not found. Please install git."
    exit 1
fi

# Check if authenticated with GitHub
if ! gh auth status &> /dev/null; then
    echo "❌ Not authenticated with GitHub. Please run: gh auth login"
    exit 1
fi

echo "✅ All prerequisites met!"

# Setup .env file
if [ ! -f ".env" ]; then
    echo "📝 Setting up environment configuration..."
    cp .env.example .env
    echo "⚠️  Please edit .env file with your tokens and configuration"
    echo "   Required: GITHUB_TOKEN, GITHUB_ORG, ANTHROPIC_API_KEY"

    read -p "📝 Open .env file now? (y/N): " edit_env
    if [ "$edit_env" = "y" ] || [ "$edit_env" = "Y" ]; then
        ${EDITOR:-nano} .env
    fi

    echo "💡 After editing .env, run this script again to continue setup"
    exit 0
fi

# Check if required env vars are set
source .env

missing_vars=()
[ -z "$GITHUB_TOKEN" ] && missing_vars+=("GITHUB_TOKEN")
[ -z "$GITHUB_ORG" ] && missing_vars+=("GITHUB_ORG")
[ -z "$ANTHROPIC_API_KEY" ] && missing_vars+=("ANTHROPIC_API_KEY")

if [ ${#missing_vars[@]} -ne 0 ]; then
    echo "❌ Missing required environment variables in .env:"
    printf '   - %s\n' "${missing_vars[@]}"
    echo "Please update .env and run setup again"
    exit 1
fi

echo "✅ Environment configuration valid!"

# Run complete workspace setup
echo "🏗️ Running complete workspace setup..."
./scripts/setup_workspace.sh

echo ""
echo "🎉 Setup Complete!"
echo "=================="
echo ""
echo "🚀 To start the orchestrator:"
echo "   docker-compose up orchestrator"
echo ""
echo "📋 To manage projects:"
echo "   python3 scripts/setup_projects.py"
echo ""
echo "🔗 To setup webhooks:"
echo "   ./scripts/setup_webhooks.sh"
echo ""
echo "📊 Monitor logs:"
echo "   docker-compose logs -f orchestrator"
echo ""
echo "Happy orchestrating! 🎼"