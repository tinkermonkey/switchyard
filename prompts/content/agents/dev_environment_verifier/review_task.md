## Your Verification Tasks

**CRITICAL**: You must verify the Docker image was built successfully and mark the container state appropriately.

### Step 1: Review Setup Agent's Work

Examine the setup agent's output for:
- Docker build commands executed
- Build success/failure messages
- Test execution and results
- Any errors or warnings

### Step 2: Inspect Docker Image

Verify the Docker image exists and is functional:

```bash
# Check if image exists (use project name from context above)
docker images {project_name}-agent:latest

# Inspect image details
docker inspect {project_name}-agent:latest
```

### Step 3: Verify Critical CLI Tools

**REQUIRED**: All agent images MUST have these CLI tools working:

```bash
# 1. Claude CLI - CRITICAL for agent execution
docker run --rm {project_name}-agent:latest which claude
docker run --rm {project_name}-agent:latest claude --version

# 2. Git CLI - CRITICAL for version control operations
docker run --rm {project_name}-agent:latest which git
docker run --rm {project_name}-agent:latest git --version

# 3. GitHub CLI - CRITICAL for GitHub API operations
docker run --rm {project_name}-agent:latest which gh
docker run --rm {project_name}-agent:latest gh --version

# 4. Basic runtime (Python, Node, etc. — depends on project)
docker run --rm {project_name}-agent:latest python3 --version 2>/dev/null || echo "Python not required"
docker run --rm {project_name}-agent:latest node --version 2>/dev/null || echo "Node not required"
```

**All three CLI tools (claude, git, gh) MUST be present and working.** If any are missing, mark as BLOCKED.

### Step 4: Validate Build Success

Confirm:
- Docker build completed without errors
- Image was created recently
- **Claude CLI is present and working** (CRITICAL)
- **Git CLI is present and working** (CRITICAL)
- **GitHub CLI is present and working** (CRITICAL)
- Project-specific runtimes work (Python, Node, etc.)
- If validation script was mentioned, it was executed and passed

### Step 5: Update Dev Container State

**CRITICAL**: You MUST update the dev container state based on your findings.

**If verification PASSES** (image built successfully and tests pass):

```python
from services.dev_container_state import dev_container_state, DevContainerStatus

project_name = "{project_name}"
image_name = f"{{project_name}}-agent:latest"

dev_container_state.set_status(
    project_name=project_name,
    status=DevContainerStatus.VERIFIED,
    image_name=image_name
)

print(f"✓ Marked {{project_name}} dev container as VERIFIED")
```

**If verification FAILS** (image not built or tests fail):

```python
from services.dev_container_state import dev_container_state, DevContainerStatus

project_name = "{project_name}"
error_message = "Brief description of why verification failed"

dev_container_state.set_status(
    project_name=project_name,
    status=DevContainerStatus.BLOCKED,
    error_message=error_message
)

print(f"✗ Marked {{project_name}} dev container as BLOCKED: {{error_message}}")
```

## Verification Decision Criteria

**APPROVED (Mark as VERIFIED)**:
- Docker image exists and was created recently
- Build output shows success (no errors)
- **Claude CLI is present and working** (`claude --version` succeeds)
- **Git CLI is present and working** (`git --version` succeeds)
- **GitHub CLI is present and working** (`gh --version` succeeds)
- Project-specific runtimes work (if applicable)
- Validation tests passed (if provided in issue)
- State was marked as VERIFIED using Python code above

**CHANGES NEEDED**:
- Image exists but CLI tools have warnings to address
- Build succeeded but tests weren't run when they should have been
- Minor issues that should be fixed

**BLOCKED (Mark as BLOCKED)**:
- Docker image doesn't exist
- Build failed with errors
- **Any of the three critical CLI tools (claude, git, gh) are missing or broken**
- Critical validation tests failed
- Cannot start container

## Review Format

IMPORTANT: Output your verification review as text directly in your response. DO NOT create any files.

```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

### Verification Results

#### Docker Image Status
- Image exists: [Yes/No]
- Image name: {project_name}-agent:latest
- Created: [timestamp if available]
- Size: [size if available]

#### Build Output Analysis
[Summary of build output — success/failure, any errors]

#### Test Results
[Results of CLI tool tests — claude, git, gh, and any project-specific tests]
- Claude CLI: [working/missing]
- Git CLI: [working/missing]
- GitHub CLI: [working/missing]
- Project runtime: [details]

#### Issues Found
[List any issues discovered, or "None" if all passed]

### Dev Container State Update
[Confirmation that state was updated to VERIFIED or BLOCKED, with Python code output]

### Summary
[Brief summary of verification decision and next steps]
```

REMEMBER: You MUST execute Python code to update the dev container state. Without this, the verification is incomplete.
