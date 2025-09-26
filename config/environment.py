import os
from pydantic import SecretStr
from pydantic_settings import BaseSettings
from typing import Optional
from pathlib import Path

class Environment(BaseSettings):
    """Centralized environment configuration"""

    # API Keys
    anthropic_api_key: Optional[SecretStr] = None
    github_token: Optional[SecretStr] = None
    openai_api_key: Optional[SecretStr] = None  # For GPT-based reviews

    # Webhook Configuration
    webhook_secret: Optional[SecretStr] = None
    github_webhook_secret: Optional[SecretStr] = None  # Alternative name
    webhook_port: int = 3000
    webhook_host: str = "0.0.0.0"

    # Redis Configuration
    redis_url: str = "redis://localhost:6379"
    redis_password: Optional[SecretStr] = None

    # Claude Configuration
    claude_model: str = "claude-3-5-sonnet-20241022"
    max_tokens: int = 100000
    temperature: float = 0.3

    # GitHub Configuration
    github_org: Optional[str] = None
    github_default_branch: str = "main"

    # ngrok Configuration
    ngrok_authtoken: Optional[SecretStr] = None

    # Project Paths
    workspace_root: Path = Path.home() / "development"
    orchestrator_root: Path = Path.cwd()

    # Monitoring
    metrics_port: int = 8000
    log_level: str = "INFO"

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