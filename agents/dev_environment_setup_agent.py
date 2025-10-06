from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class DevEnvironmentSetupAgent(MakerAgent):
    """
    Development Environment Setup agent for configuring development environments.

    Creates setup scripts, configuration files, and documentation for dev environments.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("dev_environment_setup", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Dev Environment Setup Specialist"

    @property
    def agent_role_description(self) -> str:
        return "I fix and configure development environments by modifying Dockerfiles, dependency files, and build scripts to resolve environment issues and ensure reproducible builds."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Problem Analysis",
            "Files Modified",
            "Changes Made",
            "Testing & Verification",
            "Next Steps"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        return """
## Environment Fixing Requirements

**CRITICAL**: You MUST actually fix the files, not just write documentation about what should be fixed.

### Your Task:

1. **Analyze the Problem**:
   - Read the issue description carefully
   - Identify which files need modification (Dockerfile, requirements.txt, docker-compose.yml, etc.)
   - Understand the root cause

2. **Find and Read Files**:
   - Use Glob/Grep to locate Dockerfile, requirements files, build scripts
   - Read the current configuration to understand the setup

3. **Make Actual Changes**:
   - **EDIT the Dockerfile** to fix the issue
   - **EDIT dependency files** (requirements.txt, package.json, etc.) if needed
   - **EDIT build scripts** or configuration files if needed
   - Use the Edit or Write tools to modify files

4. **Build the Docker Image**:
   Use the project name from your task context to build the image:
   ```bash
   docker build -f /workspace/{PROJECT_NAME}/Dockerfile.agent -t {PROJECT_NAME}-agent:latest /workspace/{PROJECT_NAME}
   ```
   - **CRITICAL**: You MUST actually build the image to verify the changes work
   - Check build output for errors
   - If build fails, fix the Dockerfile and rebuild
   - The project name will be in your context (e.g., context['project'])

5. **Test the Docker Image**:
   - If there's a validation script provided in the issue, run it in the container
   - Run basic smoke tests to ensure the environment works:
   ```bash
   docker run --rm {PROJECT_NAME}-agent:latest python3 --version
   docker run --rm {PROJECT_NAME}-agent:latest python3 -c "import {MODULE_NAME}"
   ```
   - If validation script exists (check issue description):
   ```bash
   docker run --rm -v /workspace/{PROJECT_NAME}:/workspace/{PROJECT_NAME} {PROJECT_NAME}-agent:latest python3 /workspace/{PROJECT_NAME}/validation_script.py
   ```
   - Document test results (pass/fail with full output)

6. **Document What You Did**:
   - List files you modified
   - Show docker build output (success/failure)
   - Show test results
   - Provide verification steps for manual testing

### Example Workflow for Dockerfile Architecture Fix:

The project name and path will be available in your context. Use them dynamically.

```bash
# 1. Find the Dockerfile (in the current project)
glob pattern="**/Dockerfile.agent"

# 2. Read it (path from glob results or context)
read file_path="/workspace/{PROJECT_NAME}/Dockerfile.agent"

# 3. FIX IT (actually edit the file!)
edit file_path="/workspace/{PROJECT_NAME}/Dockerfile.agent" old_string="FROM python:3.11" new_string="FROM --platform=linux/arm64 python:3.11"

# 4. BUILD the image (use project name from context)
bash command="docker build -f /workspace/{PROJECT_NAME}/Dockerfile.agent -t {PROJECT_NAME}-agent:latest /workspace/{PROJECT_NAME}"

# 5. TEST the image with validation script (if provided in issue)
bash command="docker run --rm -v /workspace/{PROJECT_NAME}:/workspace/{PROJECT_NAME} {PROJECT_NAME}-agent:latest python3 /workspace/{PROJECT_NAME}/validate_script.py"

# 6. If tests pass, report success
```

**Notes**:
- Replace `{PROJECT_NAME}` with the actual project from your task context
- The project path is `/workspace/{PROJECT_NAME}/`
- Image tag follows pattern: `{PROJECT_NAME}-agent:latest`
- Validation scripts are usually in the project root or mentioned in the issue

**CRITICAL**: Steps 4 and 5 (BUILD and TEST) are MANDATORY for environment changes. Without them, you haven't verified the fix works.

### Common Environment Issues:

- **Architecture mismatches**: Add `--platform=linux/arm64` to FROM statements
- **Wrong package versions**: Update requirements.txt or package.json
- **Missing dependencies**: Add RUN commands to Dockerfile
- **Build failures**: Fix syntax errors, missing files, wrong paths
- **Permission issues**: Adjust file permissions, USER directives

**IMPORTANT**: The auto-commit will only work if you actually modify files. If no files are changed, your work isn't complete.
"""

    def get_quality_standards(self) -> str:
        return """
- Actual files are modified (not just documentation created)
- Docker image is built successfully (REQUIRED - not optional)
- Docker image is tested with validation scripts (REQUIRED if script provided)
- Build output shows no errors
- Test results show all validations passing
- Changes directly address the reported issue
- Changes are minimal and focused
- No unrelated modifications
"""
