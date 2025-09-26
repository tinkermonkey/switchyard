#!/usr/bin/env python3
"""
Validation script for Phase 0 Week 3 GitHub Integration Implementation
Verifies all components are properly configured and integrated
"""

import os
import json
import yaml
from pathlib import Path
import subprocess
import sys

def check_docker_files():
    """Check that all Docker files are present and properly configured"""
    print("🐳 Checking Docker configuration...")

    # Check Dockerfile exists
    dockerfile = Path("Dockerfile")
    if not dockerfile.exists():
        print("❌ Main Dockerfile missing")
        return False

    print("✅ Main Dockerfile present")

    # Check docker-compose.yml
    compose_file = Path("docker-compose.yml")
    if not compose_file.exists():
        print("❌ docker-compose.yml missing")
        return False

    # Validate docker-compose structure
    with open(compose_file) as f:
        content = f.read()
        required_services = ['redis', 'webhook', 'orchestrator', 'ngrok']
        for service in required_services:
            if service not in content:
                print(f"❌ Missing service '{service}' in docker-compose.yml")
                return False

    print("✅ docker-compose.yml properly configured")

    # Check requirements.txt
    req_file = Path("requirements.txt")
    if not req_file.exists():
        print("❌ requirements.txt missing")
        return False

    # Check for essential dependencies
    with open(req_file) as f:
        content = f.read()
        essential_deps = ['redis', 'flask', 'requests', 'aiofiles', 'pydantic']
        for dep in essential_deps:
            if dep not in content:
                print(f"❌ Missing dependency '{dep}' in requirements.txt")
                return False

    print("✅ requirements.txt contains essential dependencies")
    return True

def check_webhook_integration():
    """Check webhook server integration with TaskQueue"""
    print("\n🔗 Checking webhook integration...")

    # Check webhook server exists
    webhook_file = Path("services/webhook_server.py")
    if not webhook_file.exists():
        print("❌ Webhook server missing")
        return False

    # Check for TaskQueue integration
    with open(webhook_file) as f:
        content = f.read()
        if "from task_queue.task_manager import Task, TaskPriority" not in content:
            print("❌ Webhook server not integrated with TaskQueue")
            return False
        if "task_queue.enqueue(task)" not in content:
            print("❌ Webhook server not enqueueing tasks properly")
            return False

    print("✅ Webhook server integrated with orchestrator TaskQueue")
    return True

def check_project_configuration():
    """Check project configuration"""
    print("\n📋 Checking project configuration...")

    # Check projects.yaml exists
    config_file = Path("config/projects.yaml")
    if not config_file.exists():
        print("❌ config/projects.yaml missing")
        return False

    # Check configuration structure
    try:
        with open(config_file) as f:
            config = yaml.safe_load(f)

        if 'projects' not in config:
            print("❌ No 'projects' section in config")
            return False

        # Check for example project with kanban columns
        for project_name, project_config in config['projects'].items():
            if 'kanban_columns' in project_config:
                print(f"✅ Project '{project_name}' has kanban column mappings")
                return True

        print("⚠️  No projects have kanban column mappings configured")
        return False

    except yaml.YAMLError as e:
        print(f"❌ Invalid YAML in projects.yaml: {e}")
        return False

def check_business_analyst_updates():
    """Check Business Analyst GitHub status updates"""
    print("\n👔 Checking Business Analyst GitHub integration...")

    # Check business analyst agent
    agent_file = Path("agents/business_analyst_agent.py")
    if not agent_file.exists():
        print("❌ Business Analyst agent missing")
        return False

    # Check for GitHub update functionality
    with open(agent_file) as f:
        content = f.read()
        if "update_github_status" not in content:
            print("❌ Business Analyst missing GitHub status updates")
            return False
        if "'gh', 'issue', 'comment'" not in content:
            print("❌ Business Analyst not configured to comment on issues")
            return False

    print("✅ Business Analyst integrated with GitHub status updates")
    return True

def check_test_scripts():
    """Check that all required test scripts exist"""
    print("\n🧪 Checking test scripts...")

    required_scripts = [
        "tests/integration/test_webhook_integration.py",
        "tests/integration/test_kanban_automation.py",
        "tests/integration/test_docker_deployment.py"
    ]

    for script_path in required_scripts:
        script_file = Path(script_path)
        if not script_file.exists():
            print(f"❌ Missing test script: {script_path}")
            return False

    print("✅ All required test scripts present")
    return True

def check_environment_template():
    """Check environment configuration template"""
    print("\n🌍 Checking environment configuration...")

    env_example = Path(".env.example")
    if not env_example.exists():
        print("❌ .env.example missing")
        return False

    # Check for required environment variables
    with open(env_example) as f:
        content = f.read()
        required_vars = [
            'GITHUB_TOKEN',
            'GITHUB_WEBHOOK_SECRET',
            'ANTHROPIC_API_KEY',
            'NGROK_AUTHTOKEN'
        ]

        for var in required_vars:
            if var not in content:
                print(f"❌ Missing environment variable '{var}' in .env.example")
                return False

    print("✅ Environment template properly configured")
    return True

def check_orchestrator_data_structure():
    """Check that orchestrator data structure is properly configured"""
    print("\n📁 Checking orchestrator data structure...")

    # Check that main.py uses new paths
    main_file = Path("main.py")
    if not main_file.exists():
        print("❌ main.py missing")
        return False

    with open(main_file) as f:
        content = f.read()
        if "orchestrator_data/state" not in content:
            print("❌ main.py not using new orchestrator_data paths")
            return False

    # Check StateManager uses new paths
    state_manager_file = Path("state_management/manager.py")
    if state_manager_file.exists():
        with open(state_manager_file) as f:
            content = f.read()
            if "orchestrator_data/state" not in content:
                print("❌ StateManager not using new orchestrator_data paths")
                return False

    print("✅ Orchestrator data structure properly configured")
    return True

def check_task_queue_compatibility():
    """Check TaskQueue compatibility with webhook server"""
    print("\n📥 Checking TaskQueue compatibility...")

    # Check TaskQueue implementation
    task_queue_file = Path("task_queue/task_manager.py")
    if not task_queue_file.exists():
        print("❌ TaskQueue implementation missing")
        return False

    with open(task_queue_file) as f:
        content = f.read()
        if "class Task" not in content:
            print("❌ Task class not defined")
            return False
        if "TaskPriority" not in content:
            print("❌ TaskPriority enum not defined")
            return False

    print("✅ TaskQueue properly implemented")
    return True

def main():
    """Run all validation checks"""
    print("🚀 Validating Phase 0 Week 3 GitHub Integration Setup\n")

    checks = [
        ("Docker Configuration", check_docker_files),
        ("Webhook Integration", check_webhook_integration),
        ("Project Configuration", check_project_configuration),
        ("Business Analyst Updates", check_business_analyst_updates),
        ("Test Scripts", check_test_scripts),
        ("Environment Template", check_environment_template),
        ("Orchestrator Data Structure", check_orchestrator_data_structure),
        ("TaskQueue Compatibility", check_task_queue_compatibility),
    ]

    passed = 0
    total = len(checks)

    for check_name, check_func in checks:
        try:
            if check_func():
                passed += 1
            else:
                print(f"\n❌ {check_name} check FAILED")
        except Exception as e:
            print(f"\n❌ {check_name} check ERROR: {e}")

    print(f"\n📊 Validation Results: {passed}/{total} checks passed")

    if passed == total:
        print("\n🎯 Week 3 GitHub Integration VALIDATION PASSED!")
        print("\n📋 Ready for deployment:")
        print("   1. Configure .env file with your tokens")
        print("   2. Update config/projects.yaml with your repositories")
        print("   3. Run: docker-compose up --build")
        print("   4. Test with: python tests/integration/test_docker_deployment.py")
        return True
    else:
        print(f"\n❌ Week 3 validation FAILED ({total - passed} issues)")
        print("\n🔧 Fix the issues above and run validation again")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)