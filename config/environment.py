import os
from pydantic import SecretStr
from pydantic_settings import BaseSettings
from typing import Optional
from pathlib import Path

class Environment(BaseSettings):
    """Centralized environment configuration"""

    # API Keys
    anthropic_api_key: Optional[SecretStr] = None
    claude_code_oauth_token: Optional[SecretStr] = None  # For Claude subscription billing
    github_token: Optional[SecretStr] = None
    openai_api_key: Optional[SecretStr] = None  # For GPT-based reviews
    context7_api_key: Optional[SecretStr] = None  # For Context7 MCP server

    # Webhook Configuration
    webhook_secret: Optional[SecretStr] = None
    github_webhook_secret: Optional[SecretStr] = None  # Alternative name
    webhook_port: int = 3000
    webhook_host: str = "0.0.0.0"

    # Redis Configuration
    redis_url: str = "redis://redis:6379"
    redis_password: Optional[SecretStr] = None

    # Claude Configuration
    claude_model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = 100000
    temperature: float = 0.3
    claude_code_weekly_token_quota: Optional[int] = 630000000  # Weekly token quota (630M tokens, resets Wed 5PM)
    claude_code_session_token_quota: Optional[int] = 50000000  # Session token quota (50M tokens per 5-hour block)

    # GitHub Configuration
    github_org: Optional[str] = None
    github_default_branch: str = "main"

    # GitHub App Authentication (optional - falls back to PAT)
    github_app_id: Optional[str] = None
    github_app_installation_id: Optional[str] = None
    github_app_private_key_path: Optional[str] = None
    github_app_private_key: Optional[SecretStr] = None

    # MCP Server Configuration
    context7_mcp_url: Optional[str] = None
    serena_mcp_url: Optional[str] = None
    puppeteer_mcp_url: Optional[str] = None

    # ngrok Configuration
    ngrok_authtoken: Optional[SecretStr] = None

    # Project Paths
    workspace_root: Path = Path.home() / "development"
    orchestrator_root: Path = Path.cwd()

    # Monitoring
    log_level: str = "INFO"

    # Performance and API Optimization
    reconciliation_freshness_hours: int = 1  # Skip reconciliation if state is fresh (default: 1 hour)

    # Worker Pool Configuration
    orchestrator_workers: int = 1  # Number of worker threads for parallel task execution (default: 1 = single-threaded)

    # Docker/Host Configuration (used by docker-compose for file permissions)
    host_uid: Optional[int] = 1000
    host_gid: Optional[int] = 1000
    docker_gid: Optional[int] = 0
    host_home: Optional[str] = None  # Host user home dir for Docker-in-Docker SSH mounts (set in .env)

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

# Create helper function to load environment
def load_environment():
    """Load environment with error handling"""
    try:
        return Environment()
    except Exception as e:
        print(f"Warning: Could not load complete environment configuration: {e}")
        # Return environment with minimal configuration for testing
        return Environment()