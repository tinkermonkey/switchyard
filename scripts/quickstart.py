#!/usr/bin/env python3
"""
Quick Start Script for Claude Code Orchestrator

This script helps set up the orchestrator with minimal configuration.
"""

import os
import sys
import subprocess
from pathlib import Path

def print_banner():
    print("""
    ╔══════════════════════════════════════════════════╗
    ║           Claude Code Orchestrator               ║
    ║              Quick Start Setup                   ║
    ╚══════════════════════════════════════════════════╝
    """)

def check_prerequisites():
    """Check if required tools are installed"""
    print("🔍 Checking prerequisites...")

    requirements = {
        'python': ['python', '--version'],
        'git': ['git', '--version'],
        'gh': ['gh', '--version']
    }

    missing = []

    for tool, cmd in requirements.items():
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"  ✅ {tool}: {result.stdout.strip()}")
            else:
                missing.append(tool)
        except FileNotFoundError:
            missing.append(tool)

    if missing:
        print(f"\n❌ Missing required tools: {', '.join(missing)}")
        print("\nPlease install the missing tools and run this script again.")
        print("See SETUP.md for installation instructions.")
        return False

    print("✅ All prerequisites found!")
    return True

def setup_environment():
    """Set up Python virtual environment and install dependencies"""
    print("\n📦 Setting up Python environment...")

    venv_path = Path("venv")

    if not venv_path.exists():
        print("  Creating virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", "venv"], check=True)

    # Determine activation script path
    if os.name == 'nt':  # Windows
        activate_script = venv_path / "Scripts" / "activate"
        pip_path = venv_path / "Scripts" / "pip"
    else:  # Unix/Linux/MacOS
        activate_script = venv_path / "bin" / "activate"
        pip_path = venv_path / "bin" / "pip"

    print("  Installing dependencies...")
    subprocess.run([str(pip_path), "install", "-r", "requirements.txt"], check=True)

    print("✅ Python environment ready!")
    return str(activate_script)

def create_env_file():
    """Create .env file from template if it doesn't exist"""
    print("\n⚙️ Setting up environment configuration...")

    env_file = Path(".env")
    template_file = Path(".env.template")

    if env_file.exists():
        print("  .env file already exists")
        return

    if not template_file.exists():
        print("  ❌ .env.template not found")
        return

    # Copy template to .env
    with open(template_file) as src, open(env_file, 'w') as dst:
        dst.write(src.read())

    print("  📝 Created .env file from template")
    print("  ⚠️  IMPORTANT: Edit .env file with your actual API keys and configuration")

def check_optional_services():
    """Check for optional services like Redis"""
    print("\n🔧 Checking optional services...")

    # Check Redis
    try:
        import redis
        r = redis.Redis(host='localhost', port=6379, socket_connect_timeout=1)
        r.ping()
        print("  ✅ Redis: Connected")
    except Exception:
        print("  ⚠️  Redis: Not available (will use in-memory fallback)")

    # Check if Claude CLI is available
    try:
        result = subprocess.run(['claude', '--version'], capture_output=True, text=True)
        if result.returncode == 0:
            print("  ✅ Claude CLI: Available")
        else:
            print("  ⚠️  Claude CLI: Not found (will use simulation mode)")
    except FileNotFoundError:
        print("  ⚠️  Claude CLI: Not found (will use simulation mode)")

def test_basic_functionality():
    """Test basic orchestrator functionality"""
    print("\n🧪 Testing basic functionality...")

    try:
        # Test imports
        sys.path.insert(0, '.')
        from agents import AGENT_REGISTRY
        from task_queue.task_manager import TaskQueue, Task, TaskPriority
        from datetime import datetime

        print(f"  ✅ Found {len(AGENT_REGISTRY)} agents")

        # Test task queue
        queue = TaskQueue()
        test_task = Task(
            id="test-001",
            agent="business_analyst",
            project="test-project",
            priority=TaskPriority.LOW,
            context={"test": True},
            created_at=datetime.now().isoformat()
        )

        queue.enqueue(test_task)
        dequeued_task = queue.dequeue()

        if dequeued_task and dequeued_task.id == "test-001":
            print("  ✅ Task queue: Working")
        else:
            print("  ❌ Task queue: Failed")

        queue.clear_all()  # Clean up

    except Exception as e:
        print(f"  ❌ Basic functionality test failed: {e}")
        return False

    return True

def show_next_steps(activate_script):
    """Show user what to do next"""
    print("""
    ╔══════════════════════════════════════════════════╗
    ║                  Setup Complete!                 ║
    ╚══════════════════════════════════════════════════╝

    🎉 Quick start setup completed successfully!

    📝 Next steps:

    1. Configure your environment:
       Edit .env file with your API keys and settings

    2. Activate virtual environment:""")

    if os.name == 'nt':
        print(f"       .\\venv\\Scripts\\activate")
    else:
        print(f"       source {activate_script}")

    print("""
    3. Start the orchestrator:
       python main.py

    4. Optional: Set up GitHub webhooks
       See SETUP.md for webhook configuration

    📚 Documentation:
    - SETUP.md - Complete setup guide
    - documentation/vision.md - Architecture overview
    - config/pipelines.yaml - Pipeline configuration

    🆘 Need help?
    - Check logs in: logs/orchestrator.log
    - Run tests: python -m pytest tests/
    - Report issues: GitHub Issues
    """)

def main():
    """Main setup function"""
    print_banner()

    # Check if we're in the right directory
    if not Path("main.py").exists():
        print("❌ Please run this script from the orchestrator root directory")
        sys.exit(1)

    # Run setup steps
    if not check_prerequisites():
        sys.exit(1)

    activate_script = setup_environment()
    create_env_file()
    check_optional_services()

    if test_basic_functionality():
        show_next_steps(activate_script)
    else:
        print("\n❌ Setup completed with errors. Check the output above.")
        sys.exit(1)

if __name__ == "__main__":
    main()