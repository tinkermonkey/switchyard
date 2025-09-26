import os
from pathlib import Path
from dotenv import load_dotenv

# Load from .env in development
env_file = Path(__file__).parent.parent / '.env'
if env_file.exists():
    load_dotenv(env_file)

class Settings:
    ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
    GITHUB_TOKEN = os.environ['GITHUB_TOKEN']
    WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'development-secret')
    
    # Project discovery
    WORKSPACE_ROOT = Path(os.environ.get('WORKSPACE_ROOT', '..'))
    
    @classmethod
    def get_project_path(cls, project_name: str) -> Path:
        return cls.WORKSPACE_ROOT / project_name