import yaml
import json
import os
from pathlib import Path
from config.environment import Environment
from pydantic import ValidationError

class ConfigurationValidator:
    def __init__(self):
        self.validation_results = {}

    def validate_environment_config(self):
        """Validate environment configuration"""
        print("🔧 Validating Environment Configuration...")

        try:
            from config.environment import load_environment
            env = load_environment()
            print("✅ Environment configuration loaded successfully")

            # Check required API keys
            api_key_ok = False
            if env.anthropic_api_key and len(env.anthropic_api_key.get_secret_value()) > 10:
                print("✅ anthropic_api_key configured")
                api_key_ok = True
            else:
                print("⚠️ anthropic_api_key missing or too short")

            github_token_ok = False
            if env.github_token and len(env.github_token.get_secret_value()) > 10:
                print("✅ github_token configured")
                github_token_ok = True
            else:
                print("⚠️ github_token missing or too short")

            webhook_secret_ok = False
            webhook_secret = env.webhook_secret or env.github_webhook_secret
            if webhook_secret and len(webhook_secret.get_secret_value()) > 5:
                print("✅ webhook_secret configured")
                webhook_secret_ok = True
            else:
                print("⚠️ webhook_secret missing or too short")

            # At least anthropic key should be present for basic functionality
            basic_config_ok = api_key_ok

            # Validate workspace paths
            if not env.workspace_root.exists():
                print(f"⚠️ Workspace root doesn't exist: {env.workspace_root}")
            else:
                print(f"✅ Workspace root exists: {env.workspace_root}")

            # Validate Redis configuration
            print(f"✅ Redis URL configured: {env.redis_url}")

            # Validate Claude configuration
            if env.claude_model:
                print(f"✅ Claude model configured: {env.claude_model}")
            else:
                print("⚠️ Claude model not specified, using default")

            # Validate GitHub configuration
            if env.github_org:
                print(f"✅ GitHub organization configured: {env.github_org}")
            else:
                print("⚠️ GitHub organization not configured")

            return basic_config_ok

        except ValidationError as e:
            print(f"❌ Environment validation failed: {e}")
            return False
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            return False

    def validate_pipeline_configuration(self):
        """Validate pipeline configuration"""
        print("⚙️ Validating Pipeline Configuration...")

        pipeline_config_path = Path("config/pipelines.yaml")
        if not pipeline_config_path.exists():
            print("❌ Pipeline configuration file missing")
            return False

        try:
            with open(pipeline_config_path) as f:
                config = yaml.safe_load(f)

            if not config:
                print("❌ Pipeline configuration is empty")
                return False

            # Validate structure
            if 'pipelines' not in config:
                print("❌ Missing 'pipelines' section")
                return False

            # Check for default pipeline specification (can be in pipelines section or root level)
            default_pipeline = None
            if 'default' in config:
                default_pipeline = config['default']
            elif 'default' in config['pipelines']:
                default_pipeline = config['pipelines']['default']

            if not default_pipeline:
                print("⚠️ No 'default' pipeline specification found")
            else:
                # Find actual pipeline configurations (exclude 'default' key)
                actual_pipelines = {k: v for k, v in config['pipelines'].items() if k != 'default'}

                if default_pipeline not in actual_pipelines:
                    print(f"❌ Default pipeline '{default_pipeline}' not found in pipelines")
                    return False

                print(f"✅ Default pipeline configured: {default_pipeline}")

            # Validate each pipeline (skip 'default' entry)
            actual_pipelines = {k: v for k, v in config['pipelines'].items() if k != 'default'}
            for pipeline_name, pipeline_config in actual_pipelines.items():
                print(f"  Validating pipeline: {pipeline_name}")

                # Check required fields
                required_fields = ['name', 'description', 'agents']
                for field in required_fields:
                    if field not in pipeline_config:
                        print(f"    ❌ Missing field: {field}")
                        return False

                # Validate agents
                if not isinstance(pipeline_config['agents'], list):
                    print(f"    ❌ agents must be a list")
                    return False

                for agent in pipeline_config['agents']:
                    if 'name' not in agent:
                        print(f"    ❌ Agent missing 'name' field")
                        return False

                    # Validate circuit breaker config if present
                    if 'circuit_breaker' in agent:
                        cb_config = agent['circuit_breaker']
                        if 'failure_threshold' not in cb_config:
                            print(f"    ❌ Circuit breaker missing 'failure_threshold'")
                            return False

                print(f"    ✅ {pipeline_name} configuration valid")

            # Validate agent_configs section if present
            if 'agent_configs' in config:
                for agent_name, agent_config in config['agent_configs'].items():
                    print(f"  Validating agent config: {agent_name}")

                    # Validate Claude model
                    if 'claude_model' in agent_config:
                        print(f"    ✅ Claude model: {agent_config['claude_model']}")

                    # Validate tools
                    if 'tools_enabled' in agent_config:
                        tools = agent_config['tools_enabled']
                        if isinstance(tools, list):
                            print(f"    ✅ {len(tools)} tools enabled")
                        else:
                            print(f"    ⚠️ tools_enabled should be a list")

            print("✅ Pipeline configuration structure valid")
            return True

        except yaml.YAMLError as e:
            print(f"❌ Pipeline configuration YAML error: {e}")
            return False

    def validate_project_configuration(self):
        """Validate project configuration"""
        print("📂 Validating Project Configuration...")

        project_config_path = Path("config/projects.yaml")
        if not project_config_path.exists():
            print("❌ Project configuration file missing")
            return False

        try:
            with open(project_config_path) as f:
                config = yaml.safe_load(f)

            if not config or 'projects' not in config:
                print("❌ Invalid project configuration structure")
                return False

            # Validate each project
            for project_name, project_config in config['projects'].items():
                print(f"  Validating project: {project_name}")

                required_fields = ['repo_url', 'local_path', 'branch']
                for field in required_fields:
                    if field not in project_config:
                        print(f"    ❌ Missing field: {field}")
                        return False

                # Validate repo URL format
                repo_url = project_config['repo_url']
                if not (repo_url.startswith('git@') or repo_url.startswith('https://')):
                    print(f"    ⚠️ Unusual repo URL format: {repo_url}")

                # Validate branch
                if not project_config['branch']:
                    print(f"    ❌ Branch cannot be empty")
                    return False

                # Check kanban configuration if present
                if 'kanban_board_id' in project_config:
                    if project_config['kanban_board_id'] and 'kanban_columns' not in project_config:
                        print(f"    ⚠️ kanban_board_id specified but no kanban_columns")

                if 'kanban_columns' in project_config:
                    columns = project_config['kanban_columns']
                    if not isinstance(columns, dict):
                        print(f"    ❌ kanban_columns must be a dictionary")
                        return False

                    agent_assigned_columns = sum(1 for agent in columns.values() if agent is not None)
                    print(f"    ✅ {len(columns)} kanban columns, {agent_assigned_columns} with agents")

                # Validate tech stack if present
                if 'tech_stacks' in project_config:
                    tech_stacks = project_config['tech_stacks']
                    if isinstance(tech_stacks, dict):
                        print(f"    ✅ Tech stacks configured: {list(tech_stacks.keys())}")
                    else:
                        print(f"    ⚠️ tech_stacks should be a dictionary")

                print(f"    ✅ {project_name} configuration valid")

            return True

        except yaml.YAMLError as e:
            print(f"❌ Project configuration YAML error: {e}")
            return False

    def validate_docker_configuration(self):
        """Validate Docker configuration"""
        print("🐳 Validating Docker Configuration...")

        config_files = {
            "docker-compose.yml": "Docker Compose configuration",
            "Dockerfile": "Main Dockerfile",
            ".env.example": "Environment template (recommended)"
        }

        all_present = True
        for file_name, description in config_files.items():
            file_path = Path(file_name)
            if file_path.exists():
                print(f"✅ {description} present")

                # Additional validation for docker-compose.yml
                if file_name == "docker-compose.yml":
                    try:
                        with open(file_path) as f:
                            compose_config = yaml.safe_load(f)

                        # Check for required services
                        if 'services' in compose_config:
                            services = compose_config['services']
                            print(f"    ✅ Services defined: {list(services.keys())}")

                            # Check for orchestrator service
                            if 'orchestrator' in services:
                                orch_service = services['orchestrator']
                                if 'volumes' in orch_service:
                                    print(f"    ✅ Orchestrator volumes configured")
                                if 'environment' in orch_service:
                                    print(f"    ✅ Orchestrator environment configured")

                            # Check for Redis service
                            if 'redis' in services:
                                print(f"    ✅ Redis service configured")
                            else:
                                print(f"    ⚠️ No Redis service found in docker-compose")

                    except yaml.YAMLError as e:
                        print(f"    ❌ Invalid YAML in docker-compose.yml: {e}")
                        all_present = False

                # Additional validation for Dockerfile
                elif file_name == "Dockerfile":
                    try:
                        with open(file_path) as f:
                            dockerfile_content = f.read()

                        if "FROM python:" in dockerfile_content:
                            print(f"    ✅ Python base image specified")
                        if "COPY requirements.txt" in dockerfile_content:
                            print(f"    ✅ Requirements.txt copy instruction found")
                        if "RUN pip install" in dockerfile_content:
                            print(f"    ✅ Package installation instruction found")

                    except Exception as e:
                        print(f"    ⚠️ Could not validate Dockerfile: {e}")

            else:
                if file_name == ".env.example":
                    print(f"⚠️ {description} missing (recommended for team setup)")
                else:
                    print(f"❌ {description} missing")
                    all_present = False

        # Check requirements.txt
        requirements_path = Path("requirements.txt")
        if requirements_path.exists():
            print("✅ requirements.txt present")
            try:
                with open(requirements_path) as f:
                    requirements = f.read().strip().split('\n')
                print(f"    ✅ {len([r for r in requirements if r.strip()])} packages specified")
            except Exception as e:
                print(f"    ⚠️ Could not read requirements.txt: {e}")
        else:
            print("❌ requirements.txt missing")
            all_present = False

        return all_present

    def validate_directory_structure(self):
        """Validate directory structure"""
        print("📁 Validating Directory Structure...")

        required_dirs = [
            ("agents", "Agent implementations"),
            ("config", "Configuration files"),
            ("monitoring", "Monitoring and logging"),
            ("pipeline", "Pipeline implementations"),
            ("resilience", "Resilience patterns"),
            ("state_management", "State management"),
            ("task_queue", "Task queue management"),
            ("scripts", "Utility scripts"),
            ("orchestrator_data", "Data directory")
        ]

        all_present = True
        for dir_name, description in required_dirs:
            dir_path = Path(dir_name)
            if dir_path.exists() and dir_path.is_dir():
                file_count = len(list(dir_path.glob("*")))
                print(f"✅ {description}: {file_count} files")
            else:
                print(f"❌ {description} directory missing")
                all_present = False

        # Check for key files
        key_files = [
            ("main.py", "Main orchestrator entry point"),
            ("docker-compose.yml", "Docker compose configuration"),
            (".env.example", "Environment template")
        ]

        for file_name, description in key_files:
            file_path = Path(file_name)
            if file_path.exists():
                print(f"✅ {description}")
            else:
                if file_name == ".env.example":
                    print(f"⚠️ {description} missing (recommended)")
                else:
                    print(f"❌ {description} missing")
                    all_present = False

        return all_present

    def validate_all_configurations(self):
        """Run complete configuration validation"""
        print("🔍 Running Complete Configuration Validation...\n")

        validations = [
            ("Environment Configuration", self.validate_environment_config),
            ("Pipeline Configuration", self.validate_pipeline_configuration),
            ("Project Configuration", self.validate_project_configuration),
            ("Docker Configuration", self.validate_docker_configuration),
            ("Directory Structure", self.validate_directory_structure)
        ]

        results = {}
        all_passed = True

        for validation_name, validation_func in validations:
            try:
                result = validation_func()
                results[validation_name] = "PASSED" if result else "FAILED"
                if not result:
                    all_passed = False
            except Exception as e:
                results[validation_name] = f"ERROR: {e}"
                all_passed = False
            print()  # Add spacing between validations

        print("📊 Configuration Validation Summary:")
        for validation, result in results.items():
            status = "✅" if "PASSED" in result else "❌"
            print(f"  {status} {validation}: {result}")

        if all_passed:
            print("\n🎉 All configurations valid!")
        else:
            print("\n⚠️ Configuration validation issues found!")

        return all_passed, results

if __name__ == "__main__":
    validator = ConfigurationValidator()
    success, results = validator.validate_all_configurations()

    if success:
        print("\n✅ All configurations valid!")
        exit(0)
    else:
        print("\n❌ Configuration validation failed!")
        exit(1)