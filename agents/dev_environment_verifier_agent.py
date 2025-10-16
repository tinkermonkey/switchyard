from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from services.dev_container_state import dev_container_state, DevContainerStatus
import logging
import json

logger = logging.getLogger(__name__)


class DevEnvironmentVerifierAgent(PipelineStage):
    """
    Dev Environment Verifier agent that validates dev environment setup.

    Verifies that Docker images were built successfully and marks them as verified.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("dev_environment_verifier", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute verification on the dev environment setup agent's output"""

        # Extract from nested task context
        task_context = context.get('context', {})
        issue = task_context.get('issue', {})
        project_name = task_context.get('project', 'unknown')

        # Get the previous stage output (from dev_environment_setup)
        previous_stage = task_context.get('previous_stage_output', '')

        if not previous_stage:
            logger.error(f"No previous_stage_output found. Task context: {json.dumps(task_context, indent=2)[:500]}")
            raise Exception("Dev Environment Verifier needs previous stage output from dev_environment_setup agent")

        # Check for review cycle context
        review_cycle = task_context.get('review_cycle', {})
        iteration_context = ""

        if review_cycle:
            iteration = review_cycle.get('iteration', 0)
            max_iterations = review_cycle.get('max_iterations', 3)
            is_rereviewing = review_cycle.get('is_rereviewing', False)

            if is_rereviewing:
                iteration_context = f"""

## Review Cycle Context - Re-Verification Mode

This is **Re-Verification Iteration {iteration} of {max_iterations}**.

**Setup Agent** has revised their work based on your previous feedback.

**Your Task**: Verify previous issues are resolved. Be concise.

**Verification Approach**:
1. Check if your PREVIOUS feedback items were addressed
2. Re-run Docker build and tests to verify fixes
3. Note any NEW issues discovered
4. Make your decision

After {max_iterations} iterations, escalates to human review.

"""
            else:
                iteration_context = f"""

## Review Cycle Context - Initial Verification

This is **Initial Verification (Iteration {iteration} of {max_iterations})**.

**Your Task**: Verify the Docker environment was built successfully and mark it as verified if all checks pass.

"""

        prompt = f"""
You are verifying the development environment setup for project: **{project_name}**

{iteration_context}

Original Issue:
Title: {issue.get('title', 'No title')}
Description: {issue.get('body', 'No description')}

Dev Environment Setup Agent's Output:
{previous_stage}

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
# Check if image exists (use project name: {project_name})
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

# 4. Basic runtime (Python, Node, etc. - depends on project)
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

IMPORTANT: Output your verification review as text directly in your response. DO NOT create any files. This review will be posted to GitHub as a comment.

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
[Summary of build output - success/failure, any errors]

#### Test Results
[Results of CLI tool tests - claude, git, gh, and any project-specific tests]
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
"""

        # Run Claude Code to perform verification
        result = await run_claude_code(prompt, context)

        # Result is the verification review in markdown format
        review_text = result if isinstance(result, str) else str(result)

        # Store the markdown output for GitHub comment
        context['markdown_review'] = review_text
        context['raw_review_result'] = review_text

        # Parse the review to determine if we should mark as VERIFIED or BLOCKED
        # Look for the Status section
        import re

        status_match = re.search(r'### Status\s*\*\*(\w+)\*\*', review_text, re.IGNORECASE)
        if status_match:
            status = status_match.group(1).upper()

            if status == 'APPROVED':
                # Mark as VERIFIED
                dev_container_state.set_status(
                    project_name=project_name,
                    status=DevContainerStatus.VERIFIED,
                    image_name=f"{project_name}-agent:latest"
                )
                logger.info(f"Marked {project_name} dev container as VERIFIED")
            elif status == 'BLOCKED':
                # Extract error message if present
                error_match = re.search(r'#### Issues Found\s*(.+?)(?=###|\Z)', review_text, re.DOTALL | re.IGNORECASE)
                error_message = error_match.group(1).strip() if error_match else "Verification failed"

                dev_container_state.set_status(
                    project_name=project_name,
                    status=DevContainerStatus.BLOCKED,
                    error_message=error_message[:200]  # Limit error message length
                )
                logger.info(f"Marked {project_name} dev container as BLOCKED: {error_message[:100]}")
        else:
            logger.warning(f"Could not parse verification status from review output for {project_name}")

        return {
            'status': 'success',
            'markdown_review': review_text,  # Primary key for review agents
            'output': review_text,  # Fallback key
            'verification_result': review_text  # Descriptive key
        }
