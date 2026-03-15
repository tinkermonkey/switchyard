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
### Your Task:

1. **Analyze the Codebase and any problem descriptions**:
   - Identify the different types of files involved in the environment setup
   - Look for sub-directories with their own Dockerfiles or dependency files and consider them too
   - Identify which files need modification (Dockerfile, requirements.txt, docker-compose.yml, etc.)

2. **Find and Read Files**:
   - Use Glob/Grep to locate Dockerfile, requirements files, build scripts
   - Read the current configuration to understand the setup

3. **Create or Fix Dockerfile.agent**:
   Follow the Dockerfile.agent Architecture Pattern below (see detailed section).

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
   - **MANDATORY**: Test all three critical CLI tools:
   ```bash
   # Test Claude CLI (REQUIRED)
   docker run --rm {PROJECT_NAME}-agent:latest which claude
   docker run --rm {PROJECT_NAME}-agent:latest claude --version
   
   # Test Git CLI (REQUIRED)
   docker run --rm {PROJECT_NAME}-agent:latest which git
   docker run --rm {PROJECT_NAME}-agent:latest git --version
   
   # Test GitHub CLI (REQUIRED)
   docker run --rm {PROJECT_NAME}-agent:latest which gh
   docker run --rm {PROJECT_NAME}-agent:latest gh --version
   ```
   - Run project-specific smoke tests:
   ```bash
   docker run --rm {PROJECT_NAME}-agent:latest python3 --version
   docker run --rm {PROJECT_NAME}-agent:latest python3 -c "import {MODULE_NAME}"
   ```
   - If validation script exists (check issue description):
   ```bash
   docker run --rm -v /workspace/{PROJECT_NAME}:/workspace/{PROJECT_NAME} {PROJECT_NAME}-agent:latest python3 /workspace/{PROJECT_NAME}/validation_script.py
   ```
   - Document test results (pass/fail with full output)
   - **If claude, git, or gh are missing, the image is NOT valid and must be fixed**

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
- The project path is `/workspace/{PROJECT_NAME}/`
- Image tag follows pattern: `{PROJECT_NAME}-agent:latest`
- Validation scripts are usually in the project root or mentioned in the issue

**CRITICAL**: Steps 4 and 5 (BUILD and TEST) are MANDATORY for environment changes. Without them, you haven't verified the fix works.

### Dockerfile.agent Architecture Pattern (MANDATORY):

**CRITICAL DESIGN PRINCIPLE**: The Dockerfile.agent builds the ENVIRONMENT (installed tools and dependencies), NOT the project source code. Source code is mounted at runtime by the orchestrator.

**Why This Matters**:
- Source code comes from a runtime mount: `-v /host/project:/workspace`
- Any source code copied during build is WASTED and gets overridden by the mount
- Copying large directories (node_modules, .git) and then chown'ing them wastes 90+ seconds

**Correct Dockerfile.agent Structure**:

```dockerfile
# ============================================================================
# Stage 1: Base Environment Setup
# ============================================================================
FROM switchyard-orchestrator:latest

# The base image already includes:
# - Claude CLI (REQUIRED - must be present)
# - Git CLI (REQUIRED - must be present)  
# - GitHub CLI (REQUIRED - must be present)
# - Python 3.11
# - Essential build tools
# - procps package (provides 'ps' command needed by Claude CLI)

USER root

# ============================================================================
# Stage 2: Install Project-Specific Runtimes
# ============================================================================
# Install language runtimes and tools needed by the project
# Examples:
# - Node.js (if building a JS/TS project)
# - Java (if building a Java project)
# - Additional Python packages (if needed beyond base image)
# - Database clients (if needed for testing)

# Example for Node.js project:
# RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\
#     apt-get install -y nodejs && \\
#     apt-get clean && \\
#     rm -rf /var/lib/apt/lists/*

# Example for enabling package managers:
# RUN corepack enable && \\
#     corepack prepare pnpm@latest --activate

# ============================================================================
# Stage 3: Pre-install Dependencies (OPTIONAL but RECOMMENDED)
# ============================================================================
# Pre-installing dependencies speeds up agent startup time because the agent
# doesn't have to install them every time it runs.

# WORKDIR should match where the orchestrator will mount the project
WORKDIR /workspace/{PROJECT_NAME}

# OPTION A: Pre-install dependencies (Recommended for fast startup)
# Copy ONLY the dependency manifests needed for installation
# DO NOT copy source code - it comes from the runtime mount

# For Node.js/pnpm projects:
# COPY package.json pnpm-workspace.yaml pnpm-lock.yaml* ./
# COPY apps/*/package.json ./apps/*/
# COPY packages/*/package.json ./packages/*/
# RUN --mount=type=cache,target=/root/.local/share/pnpm/store \\
#     pnpm install --frozen-lockfile

# For Python projects:
# COPY requirements.txt ./
# RUN pip install --no-cache-dir -r requirements.txt

# ============================================================================
# Stage 4: Ownership and Permissions
# ============================================================================
# Change ownership ONLY of what was installed, NOT source code
# Source code ownership is handled by the mount

# For pre-installed node_modules:
# RUN chown -R orchestrator:orchestrator /workspace/{PROJECT_NAME}/node_modules

# For pre-installed Python packages (already in /usr/local, no chown needed):
# (No action required - packages installed as root in system paths)

# ============================================================================
# Stage 5: Switch to Runtime User
# ============================================================================
USER orchestrator

# ============================================================================
# Stage 6: Verification (MANDATORY)
# ============================================================================
# Verify all critical CLIs are present (from base image)
RUN claude --version && \\
    git --version && \\
    gh --version

# Verify project-specific tools (if installed):
# RUN node --version && pnpm --version
# RUN python3 --version && pip --version

# ============================================================================
# Default command
# ============================================================================
CMD ["/bin/bash"]
```

**KEY RULES FOR Dockerfile.agent**:

1. **NEVER** copy the entire project with `COPY . .` - This is wasteful
2. **NEVER** chown source code - It comes from a mount at runtime
3. **DO** copy dependency manifests (package.json, requirements.txt) if pre-installing deps
4. **DO** pre-install dependencies to speed up agent startup
5. **DO** verify claude, git, and gh CLIs are present
6. **DO** use cache mounts for package managers when possible
7. **DO** clean up package manager caches to keep image size small

**Anti-Patterns to AVOID**:

❌ `COPY . .` - Copies source code (wasteful, gets overridden)
❌ `RUN chown -R orchestrator:orchestrator /workspace/{PROJECT_NAME}` - Wastes 90+ seconds
❌ Not using .dockerignore - Sends huge build context to Docker daemon
❌ Not verifying Claude CLI is present - Agent will fail at runtime
❌ Installing dependencies without cache mounts - Slow and wasteful
❌ `RUN touch /home/orchestrator/.gitconfig` - Creates a file that breaks the runtime bind mount (see .gitconfig section)
❌ `RUN git config --global ...` - Writes ~/.gitconfig into the image, same breakage (see .gitconfig section)

**Optimization: Create .dockerignore**:

Even though we're not copying source code, Docker still sends the build context.
Create a .dockerignore file to exclude unnecessary files:

```
# Exclude node_modules - they get installed during build
node_modules/

# Exclude git directory
.git/
.github/

# Exclude build artifacts
dist/
build/
.turbo/
.next/
.wrangler/

# Exclude logs
*.log

# Exclude IDE files
.vscode/
.idea/
```

This reduces build context from 770MB to <10MB and speeds up builds dramatically.

**Notes**:
- The project path is `/workspace/{PROJECT_NAME}/`
- Image tag follows pattern: `{PROJECT_NAME}-agent:latest`
- Validation scripts are usually in the project root or mentioned in the issue

**CRITICAL**: Steps 4 and 5 (BUILD and TEST) are MANDATORY for environment changes. Without them, you haven't verified the fix works.

### The .gitconfig Mount — Read This Carefully

`/home/orchestrator/.gitconfig` is a **runtime bind mount** injected by the orchestrator at container startup. It is never part of the image.

**What this means for Dockerfile.agent**:
- Do NOT create, touch, mkdir, or reference `/home/orchestrator/.gitconfig` in any Dockerfile step
- Do NOT run `git config --global` or any command that writes to `~/.gitconfig` as the orchestrator user during the build
- The base image explicitly removes `.gitconfig` before you build from it — leave that state alone

**If you encounter a `.gitconfig`-related error**:
- An error like `error mounting "/home/orchestrator/.gitconfig"` or `cannot create subdirectories in .../home/orchestrator/.gitconfig: not a directory` is a **host filesystem problem**, not an image problem
- The correct fix is to report the error in your output — do NOT attempt to fix it by modifying the Dockerfile
- The issue is that the mount source on the host is a directory instead of a file, which is a sysadmin-level fix outside your scope

**Never do any of these, no matter what**:
```
❌ RUN touch /home/orchestrator/.gitconfig
❌ RUN mkdir -p /home/orchestrator/.gitconfig
❌ RUN [ -d /home/orchestrator/.gitconfig ] && rm -rf ...
❌ RUN git config --global user.email "..."
❌ RUN git config --global user.name "..."
❌ Any RUN step that reads, writes, or tests /home/orchestrator/.gitconfig
```

Creating `.gitconfig` as a file in the image will cause ALL agent containers built from this image to fail with a Docker mount error, because the orchestrator will try to bind-mount a directory over a file.

### Common Environment Issues:

- **Architecture mismatches**: Make sure to properly sense the architecture and adapt the FROM statements, don't hard code the platform
- **Wrong package versions**: Update requirements.txt or package.json
- **Missing dependencies**: Add RUN commands to Dockerfile
- **Build failures**: Fix syntax errors, missing files, wrong paths
- **Permission issues**: Adjust file permissions, USER directives
- **Slow builds (90+ second chown)**: Dockerfile is copying source code with `COPY . .` - Remove it!
- **Large build context (500MB+)**: Missing .dockerignore file - Create one!
- **Missing CLI tools**: Verify Claude, Git, and GitHub CLIs are present with RUN verification step
- **Missing procps package**: Claude CLI needs `ps` command - Base image has it, but verify in tests

**IMPORTANT**: The auto-commit will only work if you actually modify files. If no files are changed, your work isn't complete.
"""

