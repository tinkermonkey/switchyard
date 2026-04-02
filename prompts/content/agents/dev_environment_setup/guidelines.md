---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("dev_environment_setup")
  Injected as {guidelines_section} in the initial_standard or initial_implementation mode template
variables: none
---
### Your Task:

1. **Analyse the Codebase and any problem descriptions**:
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

**CRITICAL**: Steps 4 and 5 (BUILD and TEST) are MANDATORY for environment changes. Without them, you haven't verified the fix works.

---

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
# RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
#     apt-get install -y nodejs && \
#     apt-get clean && \
#     rm -rf /var/lib/apt/lists/*

# Example for enabling package managers:
# RUN corepack enable && \
#     corepack prepare pnpm@latest --activate

# ============================================================================
# Stage 3: Pre-install Dependencies (OPTIONAL but RECOMMENDED)
# ============================================================================
WORKDIR /workspace/{PROJECT_NAME}

# OPTION A: Pre-install dependencies (Recommended for fast startup)
# Copy ONLY the dependency manifests needed for installation
# DO NOT copy source code - it comes from the runtime mount

# For Node.js/pnpm projects:
# COPY package.json pnpm-workspace.yaml pnpm-lock.yaml* ./
# COPY apps/*/package.json ./apps/*/
# RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
#     pnpm install --frozen-lockfile

# For Python projects:
# COPY requirements.txt ./
# RUN pip install --no-cache-dir -r requirements.txt

# ============================================================================
# Stage 4: Ownership and Permissions
# ============================================================================
# Change ownership ONLY of what was installed, NOT source code
# For pre-installed node_modules:
# RUN chown -R orchestrator:orchestrator /workspace/{PROJECT_NAME}/node_modules

# ============================================================================
# Stage 5: Switch to Runtime User
# ============================================================================
USER orchestrator

# ============================================================================
# Stage 6: Verification (MANDATORY)
# ============================================================================
RUN claude --version && \
    git --version && \
    gh --version

CMD ["/bin/bash"]
```

**KEY RULES FOR Dockerfile.agent**:

1. **NEVER** copy the entire project with `COPY . .` — this is wasteful
2. **NEVER** chown source code — it comes from a mount at runtime
3. **DO** copy dependency manifests (package.json, requirements.txt) if pre-installing deps
4. **DO** pre-install dependencies to speed up agent startup
5. **DO** verify claude, git, and gh CLIs are present
6. **DO** use cache mounts for package managers when possible
7. **DO** clean up package manager caches to keep image size small

**Anti-Patterns to AVOID**:

❌ `COPY . .` — copies source code (wasteful, gets overridden)
❌ `RUN chown -R orchestrator:orchestrator /workspace/{PROJECT_NAME}` — wastes 90+ seconds
❌ Not using .dockerignore — sends huge build context to Docker daemon
❌ Not verifying Claude CLI is present — agent will fail at runtime
❌ Installing dependencies without cache mounts — slow and wasteful
❌ `RUN touch /home/orchestrator/.gitconfig` — creates a file that breaks the runtime bind mount
❌ `RUN git config --global ...` — writes ~/.gitconfig into the image (same breakage)

**Optimisation: Create .dockerignore**:

Even though we're not copying source code, Docker still sends the build context.
Create a .dockerignore file to exclude unnecessary files:
```
node_modules/
.git/
.github/
dist/
build/
.turbo/
.next/
.wrangler/
*.log
.vscode/
.idea/
```
This reduces build context from 770MB to <10MB and speeds up builds dramatically.

---

### The .gitconfig Mount — Read This Carefully

`/home/orchestrator/.gitconfig` is a **runtime bind mount** injected by the orchestrator at container startup. It is never part of the image.

**What this means for Dockerfile.agent**:
- Do NOT create, touch, mkdir, or reference `/home/orchestrator/.gitconfig` in any Dockerfile step
- Do NOT run `git config --global` or any command that writes to `~/.gitconfig` as the orchestrator user during the build
- The base image explicitly removes `.gitconfig` before you build from it — leave that state alone

**If you encounter a `.gitconfig`-related error**:
- An error like `error mounting "/home/orchestrator/.gitconfig"` or `cannot create subdirectories in .../home/orchestrator/.gitconfig: not a directory` is a **host filesystem problem**, not an image problem
- The correct fix is to report the error in your output — do NOT attempt to fix it by modifying the Dockerfile

**Never do any of these, no matter what**:
```
❌ RUN touch /home/orchestrator/.gitconfig
❌ RUN mkdir -p /home/orchestrator/.gitconfig
❌ RUN [ -d /home/orchestrator/.gitconfig ] && rm -rf ...
❌ RUN git config --global user.email "..."
❌ RUN git config --global user.name "..."
❌ Any RUN step that reads, writes, or tests /home/orchestrator/.gitconfig
```

---

### Common Environment Issues:

- **Architecture mismatches**: Properly sense the architecture and adapt the FROM statements; don't hard-code the platform
- **Wrong package versions**: Update requirements.txt or package.json
- **Missing dependencies**: Add RUN commands to Dockerfile
- **Build failures**: Fix syntax errors, missing files, wrong paths
- **Permission issues**: Adjust file permissions, USER directives
- **Slow builds (90+ second chown)**: Dockerfile is copying source code with `COPY . .` — remove it
- **Large build context (500MB+)**: Missing .dockerignore file — create one
- **Missing CLI tools**: Verify Claude, Git, and GitHub CLIs are present with RUN verification step
- **Missing procps package**: Claude CLI needs `ps` command — base image has it, but verify in tests

**IMPORTANT**: The auto-commit will only work if you actually modify files. If no files are changed, your work isn't complete.
