# Adding a project to switchyard

This guide walks through every step required to bring a new repository under orchestrator management. Follow the steps in order — each section depends on the one before it.

---

## 1. Prerequisites

Before creating any configuration files, verify the following are true.

**Repository exists and is accessible:**
```bash
gh repo view <org>/<repo>
```
The repository must exist in GitHub. The orchestrator cannot create repositories.

**GitHub CLI is authenticated with the correct scopes:**
```bash
gh auth status
```
The token must include the `repo` and `project` scopes. If `project` is missing, add it at https://github.com/settings/tokens or re-authenticate:
```bash
gh auth refresh -s project
```

**The orchestrator has organization access.** If using a GitHub App, the app must be installed on the target organization. If using a PAT, the token owner must be a member of the org.

**SSH key is configured** for the repository host. The orchestrator and all agent containers authenticate via SSH — HTTPS remotes are rewritten to SSH automatically on startup, but the underlying key must be present at `~/.ssh/id_ed25519` (the path mounted into the container per `docker-compose.yml`). Agent containers have SSH keys mounted but no HTTPS credential helper, so any workspace with an HTTPS remote would fail on every git operation. The rewrite is a safety net for cases where the repo_url in the project config was accidentally specified as HTTPS, or where the repo was cloned via HTTPS by some other means.

**The orchestrator is not running** when you add a project config for the first time. The project list is loaded at startup. If the orchestrator is already running when you add the config file, restart it to pick up the new project.

---

## 2. Create the project config file

### What I do

I ask claude (launched in my local clone of codebase):

```
Add a new project to switchyard for this repo using the default workflow config: git@github.com:tinkermonkey/documentation_robotics.git
```

### Manual setup

Create a new file at:
```
config/projects/<project-name>.yaml
```

The filename (without `.yaml`) must match the `project.name` field. Use hyphens or underscores consistently — the name is used as the workspace directory name under `/workspace/` and in Docker image tags.

### Complete working example

```yaml
project:
  name: "my-project"
  description: "One-line description of the project"

  github:
    org: "my-org"
    repo: "my-project"
    repo_url: "git@github.com:my-org/my-project.git"
    branch: "main"

  tech_stacks:
    backend: "python, fastapi, postgresql"
    frontend: "react, typescript"

  testing:
    types:
      - type: "compilation"
        max_iterations: 5
        review_warnings: false
        max_file_iterations: 3

      - type: "unit"
        max_iterations: 5
        review_warnings: false
        max_file_iterations: 3

      - type: "integration"
        max_iterations: 5
        review_warnings: false
        max_file_iterations: 3

      - type: "ci"
        max_iterations: 5
        review_warnings: false
        max_file_iterations: 3

    max_file_iterations: 3
    failure_escalation_threshold: 2

  pipelines:
    enabled:
      - template: "planning_design"
        name: "planning-design"
        board_name: "Planning & Design"
        description: "Planning & Design"
        workflow: "planning_design_workflow"
        active: true

      - template: "sdlc_execution"
        name: "sdlc-execution"
        board_name: "SDLC Execution"
        description: "SDLC Execution"
        workflow: "sdlc_execution_workflow"
        active: true

      - template: "environment_support"
        name: "environment-support"
        board_name: "Environment Support"
        description: "Development Environment Support"
        workflow: "environment_support_workflow"
        active: true

  pipeline_routing:
    default_pipeline: "planning-design"
    label_routing:
      "pipeline:planning-design": "planning-design"
      "pipeline:sdlc-execution": "sdlc-execution"
      "pipeline:environment-support": "environment-support"

orchestrator:
  polling_interval: 15

  priority_mapping:
    high_priority_columns:
      - "Code Review"
      - "Testing"
    medium_priority_columns:
      - "Development"
      - "Design"
    low_priority_columns:
      - "Research"
      - "Requirements"
```

### Field reference

**`project.name`** — The project identifier. Must match the filename. Used as the workspace directory name (`/workspace/<name>/`), the Docker image tag (`<name>-agent`), and the GitHub board title prefix.

**`project.description`** — Free-text description. Shown in logs and the web UI.

**`project.github.org`** — GitHub organization or user account that owns the repository. The orchestrator uses this to scope board creation and label management.

**`project.github.repo`** — Repository name without the org prefix.

**`project.github.repo_url`** — Must be an SSH URL (`git@github.com:...`). HTTPS URLs are automatically rewritten to SSH on startup, but specifying SSH directly avoids the conversion step and is required for agent containers.

**`project.github.branch`** — The default branch. The orchestrator clones and keeps this branch updated. Agents always create feature branches off this branch.

**`project.tech_stacks`** — Free-form key/value pairs describing the technology stack. These are passed to the `dev_environment_setup` agent as context when generating `Dockerfile.agent`. The more specific you are, the better the generated environment. Any keys are valid — `backend`, `frontend`, `testing`, `infrastructure`, and so on.

**`project.testing`** — Controls the repair cycle behavior of the `testing` stage. Four test types are commonly used:
- `compilation` — build/compile checks
- `unit` — unit test suite
- `integration` — integration test suite
- `ci` — GitHub Actions / CI pipeline checks

For each type, `max_iterations` sets how many fix-and-retry cycles the agent will attempt before escalating. `review_warnings` controls whether the agent treats compiler/linter warnings as failures in addition to errors. `max_file_iterations` limits how many times the agent will attempt to fix the same file before moving on.

`failure_escalation_threshold` under the global testing settings sets how many cycle-level failures trigger escalation to human review.

**`project.pipelines.enabled`** — The list of pipeline instances active for this project. Each entry references a pipeline template (defined in `config/foundations/pipelines.yaml`), a workflow template (defined in `config/foundations/workflows.yaml`), and provides the board name that will appear in GitHub Projects v2.

The three standard pipeline entries shown in the example are used by every existing project. All three reference the canonical templates. Do not change the `template` or `workflow` values unless you are introducing a custom pipeline template.

**`project.pipeline_routing`** — Maps GitHub labels to pipeline names. The `label_routing` keys must exactly match the pipeline labels the orchestrator will create (see section 3). The values must match the `name` fields in `pipelines.enabled`.

**`orchestrator.polling_interval`** — How often (in seconds) the project monitor polls the GitHub board for card movements. The value `15` seconds is standard for all existing projects.

**`orchestrator.priority_mapping`** — Maps board columns to task queue priority levels. Tasks from `high_priority_columns` are processed before tasks from `medium_priority_columns`, which are processed before `low_priority_columns`. Column names must match the workflow template column names exactly.

### Hidden projects

To exclude a project from board reconciliation, monitoring, and the web UI, add `hidden: true` under `project`:

```yaml
project:
  name: "test-project"
  hidden: true
  ...
```

Hidden projects are loaded by the config manager but excluded from `list_visible_projects()`, which is the call used by startup reconciliation, the project monitor, and the web UI.

---

## 3. GitHub setup

### What the orchestrator creates automatically

On startup, the orchestrator runs `reconcile_project()` for every visible project. For a new project, this creates:

**Three GitHub Projects v2 boards** (one per enabled pipeline), named:
- `<project-name> - Planning & Design`
- `<project-name> - SDLC Execution`
- `<project-name> - Development Environment Support`

Each board is created at the organization level and then linked to the repository so it appears under the repository's Projects tab.

**Board columns** matching the workflow template. For the three standard workflows:

| Board | Columns |
|---|---|
| Planning & Design | Backlog, Research, Requirements, Design, Work Breakdown, In Development, In Review, Done |
| SDLC Execution | Backlog, Development, Code Review, Testing, Staged, Done |
| Environment Support | Backlog, In Progress, Verification, Done |

**Repository labels** for pipeline routing and stage tracking. The reconciler creates these labels with `--force`, so they will be created if missing and updated if they exist with different colors or descriptions. Labels created include:
- `pipeline:planning-design`
- `pipeline:sdlc-execution`
- `pipeline:environment-support`
- `stage:research`, `stage:requirements`, `stage:design`, `stage:work-breakdown`
- `stage:implementation`, `stage:implementation_review`, `stage:testing`
- `stage:environment-setup`, `stage:environment-verification`, `stage:pr-review`
- `approved` (used by the git workflow automation)

State for all boards and labels is written to `state/projects/<project-name>/github_state.yaml`. Do not edit this file manually.

### What you must do manually

**Nothing is required before first startup.** The reconciler is designed to create all boards and labels from scratch.

However, if you want issues to appear on the correct board without manual board assignment in the GitHub UI, you can add issues to the board's Backlog column at any time after the boards exist. The orchestrator does not auto-add issues to boards — that is a manual action or can be done via GitHub automation rules you configure separately.

> **Note:** If the reconciler fails to create a board (for example, due to a permissions issue), it logs a diagnostic report showing the specific GitHub API error, authentication status, and organization access check. See section 6 for how to read those logs.

---

## 4. Dockerfile.agent

### What it is

`Dockerfile.agent` is a Docker image definition that lives in the root of the managed project's repository. The orchestrator builds this image to create the isolated environment in which agents (`senior_software_engineer`, `code_reviewer`, `pr_code_reviewer`, `requirements_verifier`, `claude_advisor`) run.

The image extends `switchyard-orchestrator:latest` — the orchestrator's own image — which already contains Claude CLI, Git, GitHub CLI, Python 3.11, and Docker CLI. `Dockerfile.agent` only needs to add the project's own dependencies on top of that base.

Source code is not baked into the image. The project workspace is mounted at runtime via:
```
docker run -v /workspace/<project-name>:/workspace/<project-name> ...
```

### Where to create it

Create `Dockerfile.agent` in the root of the managed project's repository (not in the switchyard repository):
```
/workspace/<project-name>/Dockerfile.agent
```

For a Python project, commit it to the project's `main` branch so it is present when the orchestrator clones or pulls the repository.

### Minimum viable Dockerfile.agent for a Python project

```dockerfile
FROM switchyard-orchestrator:latest

USER root

WORKDIR /workspace/<project-name>

COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

USER orchestrator

RUN claude --version && \
    git --version && \
    gh --version && \
    python3 --version && \
    docker --version

CMD ["/bin/bash"]
```

Replace `<project-name>` with the actual project name. The verification `RUN` step at the end is mandatory — the `dev_environment_verifier` agent checks that these commands succeed to mark the image as verified.

### For Node.js projects

```dockerfile
FROM switchyard-orchestrator:latest

USER root

WORKDIR /workspace/<project-name>

COPY package.json package-lock.json ./

RUN npm ci

USER orchestrator

RUN claude --version && \
    git --version && \
    gh --version && \
    node --version && \
    docker --version

CMD ["/bin/bash"]
```

### What the orchestrator does with it

At startup, `ProjectWorkspaceManager.initialize_all_projects()` checks each project directory for `Dockerfile.agent`. If the file is missing, the project is flagged as needing setup. The orchestrator then enqueues a `dev_environment_setup` task at HIGH priority.

The `dev_environment_setup` agent:
1. Analyzes the project's dependencies and tech stack.
2. Generates or updates `Dockerfile.agent` in the project workspace.
3. Runs `docker build` to build the image tagged as `<project-name>-agent`.
4. Posts the result as a GitHub issue comment.

The `dev_environment_verifier` agent then:
1. Attempts to run the image and verify the CLI tools.
2. Writes a verification record to `state/dev_containers/<project-name>_verified.yaml` on success.

Once verified, subsequent startups skip the setup step unless the Docker image is missing from the local Docker daemon.

If you provide a working `Dockerfile.agent` in the repository before the orchestrator first runs against the project, the generated setup step will update it based on the actual dependency files rather than starting from scratch. For complex projects with known-good dependency files, committing a working `Dockerfile.agent` upfront saves one setup cycle.

---

## 5. First startup behavior

When the orchestrator starts with a new project in `config/projects/`, it performs these steps in order:

1. **Workspace initialization** — `ProjectWorkspaceManager.initialize_all_projects()` iterates over all visible projects. For the new project, it attempts to find the checkout at `/workspace/<project-name>/`. If found, it runs `git fetch origin` and `git pull --ff-only`. If not found, it clones from `repo_url`. After cloning or finding the checkout, it rewrites any HTTPS remote URL to SSH.

2. **Dev environment setup check** — After workspace init, the orchestrator checks whether `Dockerfile.agent` exists in the project directory and whether the Docker image is already marked as verified. If either is false, a `dev_environment_setup` task is queued at HIGH priority.

3. **Board reconciliation** — For each visible project, the orchestrator calls `reconcile_project()`. For the new project, this creates the three GitHub Projects v2 boards (or discovers them if they already exist), configures their columns, links them to the repository, and creates all repository labels.

4. **Board rescan** — The project monitor performs a full scan of all active boards to detect any cards already in actionable columns, then begins its 15-second polling loop.

The `dev_environment_setup` task runs outside of Docker (it needs to build Docker images). It reads `project.tech_stacks` from the project config, inspects the project's dependency files (`requirements.txt`, `package.json`, etc.), generates `Dockerfile.agent`, builds the image, and records the result.

---

## 6. Verifying the project is configured correctly

### Check that boards were created

```bash
gh project list --owner <org>
```

You should see three boards prefixed with the project name:
```
<project-name> - Planning & Design
<project-name> - SDLC Execution
<project-name> - Development Environment Support
```

To see a board's columns:
```bash
gh project field-list <project-number> --owner <org> --format json | jq '.fields[] | select(.name == "Status") | .options[].name'
```

### Check that labels were created

```bash
gh label list --repo <org>/<repo>
```

You should see `pipeline:planning-design`, `pipeline:sdlc-execution`, `pipeline:environment-support`, and the `stage:*` labels.

### Check the GitHub state file

```bash
cat state/projects/<project-name>/github_state.yaml
```

A fully reconciled project will have `project_id`, `status_field_id`, and column entries populated for each board. An incomplete reconciliation leaves these fields empty or missing.

### Check that the workspace was initialized

Inside the orchestrator container:
```bash
ls /workspace/<project-name>/
```

Or on the host (adjust path based on your `docker-compose.yml`):
```bash
ls ../  <project-name>/
```

### Check dev environment setup status

```bash
cat state/dev_containers/<project-name>_verified.yaml
```

A verified project shows `verified: true` and a timestamp. If this file does not exist or shows `verified: false`, the setup is still in progress or failed.

### Watch the logs during startup

```bash
docker-compose logs -f orchestrator
```

Key log lines to look for:
```
Project <name> found at /workspace/<name>
Creating project board: Planning & Design (#<number>)
Linked project #<number> to repository <repo>
Created label: pipeline:planning-design
Successfully reconciled project: <name>
Queuing dev_environment_setup task for <name>
```

If board creation fails, the orchestrator logs a diagnostic block that includes authentication status, organization access check, and the specific API error. Look for lines prefixed with `AUTOMATIC DIAGNOSTICS:`.

### Health check

```bash
curl http://localhost:5001/health
```

```bash
curl http://localhost:5001/api/projects
```

The `/api/projects` endpoint lists all active projects with their current status.

---

## 7. Triggering the first pipeline run

The orchestrator does not auto-create issues. You create issues in GitHub and move them to a board column to trigger an agent.

### For the planning pipeline (epics and features)

1. Create a GitHub issue in the repository with a title and description of the feature or epic.
2. Add the issue to the "Planning & Design" board.
3. Add the label `pipeline:planning-design` to the issue.
4. Move the issue to the "Research" column on the Planning & Design board.

The project monitor detects the move within 15 seconds and enqueues a task for the `idea_researcher` agent.

### For the SDLC execution pipeline (implementation tasks)

1. Create a GitHub issue describing a specific implementation task.
2. Add the issue to the "SDLC Execution" board.
3. Add the label `pipeline:sdlc-execution` to the issue.
4. Move the issue to the "Development" column.

The project monitor detects the move and enqueues a task for the `senior_software_engineer` agent.

> **Note:** The SDLC execution pipeline requires a working `Dockerfile.agent` and a verified Docker image. If the dev environment setup has not completed successfully, the agent task will fail at container launch. Wait for the `dev_environment_verifier` to write a successful verification before moving implementation issues to the Development column.

### For environment troubleshooting

1. Create a GitHub issue describing the environment problem.
2. Add the issue to the "Environment Support" board.
3. Move the issue to the "In Progress" column.

This triggers the `dev_environment_setup` agent to analyze and repair the environment.

---

## 8. Common setup mistakes and how to fix them

### Board creation fails with "Resource not accessible by personal access token"

The GitHub token is missing the `project` scope.

```bash
gh auth refresh -s project
gh auth status
```

Confirm the output shows `project` in the token scopes, then restart the orchestrator.

### Board creation fails with "Not Found" for the organization

Either the org name is wrong or the authenticated user is not a member.

```bash
gh api orgs/<org>
```

If this returns a 404, check the `org` field in the project config. If it returns organization data but board creation still fails, the user needs organization-level project creation permissions (organization owner or project creation enabled in org settings).

### The workspace is not being cloned

If the orchestrator logs `Failed to clone <project-name>` and you are running in Docker Compose mode, the project directory on the host must exist at the path that maps to `/workspace/<project-name>/` inside the container.

Per `docker-compose.yml`, `/workspace/` inside the container maps to `../` (the parent of the `switchyard/` directory) on the host. Create the directory on the host and clone the repository there:

```bash
cd ..
git clone git@github.com:<org>/<repo>.git <project-name>
```

Then restart the orchestrator. It will find the existing checkout and run `git pull`.

### The remote URL was HTTPS, agents fail to push

The orchestrator rewrites HTTPS remotes to SSH on startup. However, if the SSH key at `~/.ssh/id_ed25519` cannot authenticate to GitHub (wrong key or key not added to the GitHub account/org), all git operations from agents will fail.

Verify the key works:
```bash
ssh -T git@github.com
```

Expected output: `Hi <username>! You've successfully authenticated...`

### Dev environment setup keeps failing

Check the issue comment left by the `dev_environment_setup` agent — it posts the full error output to GitHub. Common causes:

- The project has no `requirements.txt` or `package.json` at the expected location. The agent searches the workspace root. If your dependencies file is in a subdirectory, the `Dockerfile.agent` must be written manually to copy from that path.
- The base image `switchyard-orchestrator:latest` does not exist locally. This means the orchestrator itself has not been built. Run `docker-compose build` first.
- A dependency fails to install inside the container. The agent retries up to the configured limit and then posts the failure. Fix the dependency issue in the project's dependency files, then either wait for the next setup attempt or manually move a new issue to the Environment Support board's "In Progress" column.

### Labels exist but issues are not routing to the right pipeline

The label name in the issue must exactly match an entry in `pipeline_routing.label_routing`. Check for typos:

```bash
gh label list --repo <org>/<repo> | grep pipeline
```

The label names are case-sensitive. The orchestrator creates `pipeline:planning-design` (lowercase, with colon and hyphen). If you applied a label with different capitalization, remove it and apply the correct one.

### State file has empty board IDs after reconciliation

This indicates the board was created but column configuration failed. Typical cause: the `Status` field options could not be written via the GraphQL API.

Delete the state file and force re-reconciliation:
```bash
rm state/projects/<project-name>/github_state.yaml
```

Restart the orchestrator. It will attempt to discover the existing boards by name and reconfigure their columns. If this fails again, check the orchestrator logs for the `UpdateProjectV2Field` GraphQL mutation error.

### Project is not appearing in the web UI

Check whether `hidden: true` is set in the config file. Only projects with `hidden: false` (the default) are included in `list_visible_projects()` and shown in the UI and monitoring.
