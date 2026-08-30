---
invoked_by: prompts/builder.py — PromptBuilder.build_verifier_prompt() via loader.agent_review_task("dev_environment_verifier")
  Injected as {verification_task} in the verifier mode template; all {project_name} occurrences
  are pre-expanded via .replace("{project_name}", project_name) before injection
variables:
  project_name: Project name string; expanded by build_verifier_prompt() before template injection
    (not a standard str.format() variable — replaced via str.replace() to avoid conflicts with
    shell command braces like ${VAR} in the file content)
---
## Your Verification Tasks

**CRITICAL**: You must verify the Docker image was built successfully and mark the container state appropriately.

### Step 0: Check For A REQUIRED FIX In The Original Issue

If the Original Issue description above contains a `## REQUIRED FIX` section, that is the
actual reason this rebuild was triggered — a specific automated test is failing because
of it. This takes priority over the generic checks below:

- Confirm the change was actually made: check `git diff` / `git log -1 --stat` against the
  project's working tree and verify the file(s) the REQUIRED FIX names were edited and
  committed.
- Do NOT accept the setup agent's narrative summary as proof. If its output claims "no
  change was needed" or "the issue was already resolved," treat that as a red flag
  requiring extra scrutiny, not confirmation — independently re-run the *exact* failing
  command described in the REQUIRED FIX (in the actual environment the failing test uses)
  and verify it now succeeds yourself.
- If the required fix was not made, or was made but you cannot independently confirm it
  resolves the described failure, mark **BLOCKED** regardless of whether the image
  otherwise builds and the CLI tools are present.

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
- **If a `## REQUIRED FIX` was named in the Original Issue: the named file(s) were edited
  and committed, AND you personally re-ran the exact failing command and saw it succeed
  (see Step 0) — do not approve on the image/CLI checks alone**
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
- **A `## REQUIRED FIX` was named in the Original Issue but was not made, or was made but
  you could not independently confirm it resolves the described failure (see Step 0)**

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
