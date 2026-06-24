"""
Switchyard MCP server.

Exposes structured tools for managing GitHub project issues through configured
workflows and diagnosing pipeline runs.  Designed as a specialist node that
phone-home (or any MCP client) can call over Streamable HTTP.

Auth:  Bearer token in SWITCHYARD_MCP_TOKEN env var; checked on all /mcp/* routes.
Port:  5002 (configured in docker-compose.yml).

Run:   python mcp/server.py   (from /app working directory)

NOTE: This file lives in /app/mcp/ but does NOT have an __init__.py alongside
it.  That is intentional — it prevents /app/mcp/ from being treated as a
regular Python package, which would shadow the installed `mcp` SDK when
PYTHONPATH=/app is set.  Sibling imports (auth) resolve because Python adds
the script's own directory (/app/mcp) to sys.path[0] when run directly.
"""

import asyncio
import json
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import redis as redis_lib
import yaml
from elasticsearch import Elasticsearch
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from auth import BearerAuthMiddleware

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger(__name__)

SERVER_VERSION = "0.1.0"

# ── Paths & external service URLs ─────────────────────────────────────────────

APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
WORKFLOWS_YAML = APP_ROOT / "config" / "foundations" / "workflows.yaml"
PROJECTS_CONFIG_DIR = APP_ROOT / "config" / "projects"
STATE_DIR = APP_ROOT / "state" / "projects"
OBSERVABILITY_URL = os.environ.get("OBSERVABILITY_URL", "http://observability-server:5001")

# ── Lazy Redis / Elasticsearch clients ────────────────────────────────────────

_redis: redis_lib.Redis | None = None
_es: Elasticsearch | None = None


def _get_redis() -> redis_lib.Redis:
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL", "redis://redis:6379")
        _redis = redis_lib.Redis.from_url(url, decode_responses=True)
    return _redis


def _get_es() -> Elasticsearch | None:
    global _es
    if _es is None:
        try:
            _es = Elasticsearch(["http://elasticsearch:9200"])
        except Exception as exc:
            log.warning("Elasticsearch not available: %s", exc)
    return _es


# ── FastMCP server setup ───────────────────────────────────────────────────────

mcp = FastMCP("switchyard")
# Mount at "/" so the endpoint lands at /mcp (not /mcp/mcp).
mcp.settings.streamable_http_path = "/"
# DNS rebinding protection disabled — bearer token is the access gate.
mcp.settings.transport_security.enable_dns_rebinding_protection = False

mcp_starlette = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_starlette.router.lifespan_context(app):
        yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(BearerAuthMiddleware, protected_prefix="/mcp")
app.mount("/mcp", mcp_starlette)


@app.get("/health")
async def health():
    return {"status": "ok", "server_version": SERVER_VERSION}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load_workflows() -> dict:
    with open(WORKFLOWS_YAML) as f:
        data = yaml.safe_load(f)
    return data.get("workflow_templates", {})


def _load_project_config(project: str) -> dict:
    path = PROJECTS_CONFIG_DIR / f"{project}.yaml"
    if not path.exists():
        raise ValueError(f"Project config not found: '{project}'. "
                         f"Available: {[p.stem for p in PROJECTS_CONFIG_DIR.glob('*.yaml')]}")
    with open(path) as f:
        return yaml.safe_load(f)


def _load_project_state(project: str) -> dict | None:
    state_file = STATE_DIR / project / "github_state.yaml"
    if not state_file.exists():
        return None
    with open(state_file) as f:
        return yaml.safe_load(f)


def _project_github(config: dict) -> tuple[str, str]:
    """Return (org, repo) from a project config dict."""
    gh = config.get("project", {}).get("github", {})
    return gh.get("org", ""), gh.get("repo", "")


def _gh_graphql(query: str) -> dict:
    """Execute a GraphQL query via the gh CLI and return parsed JSON."""
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh graphql failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


def _elapsed(started_at: str | None, ended_at: str | None = None) -> str | None:
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(
            started_at.rstrip("Z")
        ).replace(tzinfo=timezone.utc)
        end = (
            datetime.fromisoformat(ended_at.rstrip("Z")).replace(tzinfo=timezone.utc)
            if ended_at
            else datetime.now(timezone.utc)
        )
        secs = int((end - start).total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return None


def _redis_get_run(pipeline_run_id: str, r: redis_lib.Redis) -> dict | None:
    raw = r.get(f"orchestrator:pipeline_run:{pipeline_run_id}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


# ── MCP tools: issue / project management ─────────────────────────────────────

@mcp.tool()
async def get_workflow_statuses(project: str, workflow: str) -> dict:
    """
    Return ordered column names and metadata for a named workflow.

    Args:
        project: Project name (must match a file under config/projects/).
        workflow: Workflow key from config/foundations/workflows.yaml, e.g.
                  planning_design_workflow, sdlc_execution_workflow,
                  environment_support_workflow.
    """
    workflows = _load_workflows()
    wf = workflows.get(workflow)
    if not wf:
        available = list(workflows.keys())
        raise ValueError(
            f"Workflow '{workflow}' not found. Available: {available}"
        )
    return {
        "workflow": workflow,
        "name": wf.get("name"),
        "description": wf.get("description"),
        "columns": [
            {
                "name": col.get("name"),
                "agent": col.get("agent"),
                "type": col.get("type"),
                "stage_mapping": col.get("stage_mapping"),
                "description": col.get("description"),
            }
            for col in wf.get("columns", [])
        ],
        "pipeline_trigger_columns": wf.get("pipeline_trigger_columns", []),
        "pipeline_exit_columns": wf.get("pipeline_exit_columns", []),
    }


@mcp.tool()
async def list_issues(
    project: str,
    workflow: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """
    List issues on a project board, optionally filtered by workflow and/or
    column status.  Returns up to 100 items per board.

    Args:
        project: Project name.
        workflow: Restrict to the board mapped to this workflow key
                  (e.g. sdlc_execution_workflow).  None returns issues across
                  all boards for the project.
        status:  Column name to filter by (e.g. "Development", "Code Review").
                 None returns issues in all columns.
    """
    state = _load_project_state(project)
    if not state:
        raise ValueError(
            f"No GitHub state found for project '{project}'. "
            "Has the board been reconciled?"
        )
    config = _load_project_config(project)

    # Determine which boards to query.
    state_boards: dict = state.get("boards", {})
    if workflow:
        pipelines = config.get("project", {}).get("pipelines", {}).get("enabled", [])
        board_names = {
            p["board_name"]
            for p in pipelines
            if p.get("workflow") == workflow and "board_name" in p
        }
        boards_to_query = [
            (name, board)
            for name, board in state_boards.items()
            if name in board_names
        ]
    else:
        boards_to_query = list(state_boards.items())

    if not boards_to_query:
        return []

    r = _get_redis()
    results: list[dict] = []

    for board_name, board in boards_to_query:
        project_id = board.get("project_id")
        if not project_id:
            continue

        query = f'''{{
          node(id: "{project_id}") {{
            ... on ProjectV2 {{
              items(first: 100) {{
                nodes {{
                  content {{
                    ... on Issue {{
                      number
                      title
                      url
                      state
                      labels(first: 10) {{ nodes {{ name }} }}
                      assignees(first: 5) {{ nodes {{ login }} }}
                    }}
                  }}
                  fieldValueByName(name: "Status") {{
                    ... on ProjectV2ItemFieldSingleSelectValue {{
                      name
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}'''

        try:
            data = _gh_graphql(query)
        except Exception as exc:
            log.warning("Failed to query board '%s': %s", board_name, exc)
            continue

        items = (
            data.get("data", {})
                .get("node", {})
                .get("items", {})
                .get("nodes", [])
        )

        for item in items:
            content = item.get("content") or {}
            issue_number = content.get("number")
            if not issue_number:
                continue  # draft / PR items

            col_status = (item.get("fieldValueByName") or {}).get("name")
            if status and col_status != status:
                continue

            pipeline_run_id = r.hget(
                "orchestrator:pipeline_run:issue_mapping",
                f"{project}:{issue_number}",
            )
            results.append({
                "issue_number": issue_number,
                "title": content.get("title"),
                "url": content.get("url"),
                "state": content.get("state"),
                "board": board_name,
                "status": col_status,
                "labels": [
                    lbl["name"]
                    for lbl in content.get("labels", {}).get("nodes", [])
                ],
                "assignees": [
                    a["login"]
                    for a in content.get("assignees", {}).get("nodes", [])
                ],
                "pipeline_run_id": pipeline_run_id,
            })

    return results


@mcp.tool()
async def get_issue(issue_number: int, project: str) -> dict:
    """
    Return metadata for a specific issue: current board column(s), pipeline run
    state, labels, assignees, and the three most recent comments.

    Args:
        issue_number: GitHub issue number.
        project: Project name.
    """
    config = _load_project_config(project)
    org, repo = _project_github(config)

    query = f'''{{
      repository(owner: "{org}", name: "{repo}") {{
        issue(number: {issue_number}) {{
          title
          url
          state
          createdAt
          updatedAt
          labels(first: 20) {{ nodes {{ name color }} }}
          assignees(first: 10) {{ nodes {{ login }} }}
          comments(last: 3) {{
            nodes {{
              author {{ login }}
              body
              createdAt
              url
            }}
          }}
          projectItems(first: 10) {{
            nodes {{
              id
              project {{ number title }}
              fieldValueByName(name: "Status") {{
                ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
              }}
            }}
          }}
        }}
      }}
    }}'''

    data = _gh_graphql(query)
    issue = (
        data.get("data", {})
            .get("repository", {})
            .get("issue")
    )
    if not issue:
        raise ValueError(
            f"Issue #{issue_number} not found in {org}/{repo}"
        )

    board_statuses = [
        {
            "board_number": item.get("project", {}).get("number"),
            "board_name": item.get("project", {}).get("title"),
            "item_id": item.get("id"),
            "status": (item.get("fieldValueByName") or {}).get("name"),
        }
        for item in issue.get("projectItems", {}).get("nodes", [])
    ]

    r = _get_redis()
    pipeline_run_id = r.hget(
        "orchestrator:pipeline_run:issue_mapping",
        f"{project}:{issue_number}",
    )
    pipeline_run: dict | None = None
    if pipeline_run_id:
        pipeline_run = _redis_get_run(pipeline_run_id, r)

    return {
        "issue_number": issue_number,
        "title": issue.get("title"),
        "url": issue.get("url"),
        "state": issue.get("state"),
        "created_at": issue.get("createdAt"),
        "updated_at": issue.get("updatedAt"),
        "labels": [lbl["name"] for lbl in issue.get("labels", {}).get("nodes", [])],
        "assignees": [a["login"] for a in issue.get("assignees", {}).get("nodes", [])],
        "board_statuses": board_statuses,
        "recent_comments": [
            {
                "author": (c.get("author") or {}).get("login"),
                # Trim long agent comments to keep the response manageable.
                "body": c.get("body", "")[:600],
                "created_at": c.get("createdAt"),
                "url": c.get("url"),
            }
            for c in issue.get("comments", {}).get("nodes", [])
        ],
        "pipeline_run_id": pipeline_run_id,
        "pipeline_run": pipeline_run,
    }


@mcp.tool()
async def move_issue(
    issue_number: int,
    project: str,
    to_status: str,
    from_status: str | None = None,
) -> dict:
    """
    Move an issue to a named column on its GitHub Projects v2 board.

    Wraps updateProjectV2ItemFieldValue using field/option IDs stored in the
    project's github_state.yaml.  The orchestrator's GitHubAPIClient rate
    limiting is NOT applied here — this tool is intended for low-frequency
    manual corrections, not bulk automation.

    Args:
        issue_number: GitHub issue number.
        project: Project name.
        to_status: Target column name (e.g. "Development", "Code Review").
        from_status: Optional guard — if provided and the issue is NOT currently
                     in this column, the move is rejected with an error.
    """
    config = _load_project_config(project)
    state = _load_project_state(project)
    if not state:
        raise ValueError(f"No GitHub state for project '{project}'.")

    org, repo = _project_github(config)

    # Find which board exposes to_status as a valid column.
    board_match: tuple[str, dict] | None = None
    for board_name, board in state.get("boards", {}).items():
        if any(c.get("name") == to_status for c in board.get("columns", [])):
            board_match = (board_name, board)
            break

    if not board_match:
        all_columns = sorted({
            c["name"]
            for b in state.get("boards", {}).values()
            for c in b.get("columns", [])
            if c.get("name")
        })
        raise ValueError(
            f"Column '{to_status}' not found in any board for project '{project}'. "
            f"Available columns: {all_columns}"
        )

    board_name, board = board_match
    project_id = board.get("project_id")
    project_number = board.get("project_number")
    status_field_id = board.get("status_field_id")

    if not status_field_id:
        raise ValueError(
            f"status_field_id missing from state for board '{board_name}'. "
            "The board may need to be re-reconciled."
        )

    column_option_id: str | None = None
    for col in board.get("columns", []):
        if col.get("name") == to_status:
            column_option_id = col.get("id")
            break

    if not column_option_id:
        raise ValueError(
            f"Column '{to_status}' found in board '{board_name}' "
            "but has no option ID — board may need re-reconciliation."
        )

    # Fetch the project item ID and current status for this issue.
    query = f'''{{
      repository(owner: "{org}", name: "{repo}") {{
        issue(number: {issue_number}) {{
          projectItems(first: 10) {{
            nodes {{
              id
              project {{ number }}
              fieldValueByName(name: "Status") {{
                ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
              }}
            }}
          }}
        }}
      }}
    }}'''

    data = _gh_graphql(query)
    project_items = (
        data.get("data", {})
            .get("repository", {})
            .get("issue", {})
            .get("projectItems", {})
            .get("nodes", [])
    )

    item_id: str | None = None
    current_status: str | None = None
    for item in project_items:
        if item.get("project", {}).get("number") == project_number:
            item_id = item.get("id")
            current_status = (item.get("fieldValueByName") or {}).get("name")
            break

    if not item_id:
        raise ValueError(
            f"Issue #{issue_number} is not on board '{board_name}' "
            f"(project #{project_number})."
        )

    if from_status is not None and current_status != from_status:
        raise ValueError(
            f"Guard check failed: issue #{issue_number} is currently in "
            f"'{current_status}', not '{from_status}'."
        )

    # Execute the mutation with up to 3 retries (matching pipeline_progression.py).
    mutation = f'''
      mutation {{
        updateProjectV2ItemFieldValue(
          input: {{
            projectId: "{project_id}"
            itemId: "{item_id}"
            fieldId: "{status_field_id}"
            value: {{ singleSelectOptionId: "{column_option_id}" }}
          }}
        ) {{
          projectV2Item {{ id }}
        }}
      }}
    '''

    last_err: str = ""
    for attempt in range(3):
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={mutation}"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            resp = json.loads(result.stdout)
            if "errors" not in resp:
                break
            last_err = str(resp["errors"])
        else:
            last_err = result.stderr.strip()
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"Move failed after 3 attempts: {last_err}")

    return {
        "success": True,
        "issue_number": issue_number,
        "project": project,
        "board": board_name,
        "from_status": current_status,
        "to_status": to_status,
    }


# ── MCP tools: pipeline run diagnostics ───────────────────────────────────────

@mcp.tool()
async def list_active_runs(project: str | None = None) -> list[dict]:
    """
    Return all in-progress pipeline runs, optionally filtered by project.

    Reads the orchestrator's Redis issue→run mapping and returns runs whose
    status is 'active' or 'feedback_listening'.

    Args:
        project: If given, restrict to this project name.  None returns runs
                 across all projects.
    """
    r = _get_redis()
    all_mappings: dict[str, str] = r.hgetall(
        "orchestrator:pipeline_run:issue_mapping"
    )

    results: list[dict] = []
    seen: set[str] = set()

    for issue_key, run_id in all_mappings.items():
        proj, _, _ = issue_key.partition(":")
        if project and proj != project:
            continue
        if run_id in seen:
            continue
        seen.add(run_id)

        run = _redis_get_run(run_id, r)
        if not run:
            continue
        if run.get("status") not in ("active", "feedback_listening"):
            continue

        results.append({
            "pipeline_run_id": run.get("id"),
            "project": run.get("project"),
            "board": run.get("board"),
            "issue_number": run.get("issue_number"),
            "issue_title": run.get("issue_title"),
            "status": run.get("status"),
            "started_at": run.get("started_at"),
            "elapsed": _elapsed(run.get("started_at")),
        })

    results.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return results


@mcp.tool()
async def get_run_status(pipeline_run_id: str) -> dict:
    """
    Return detailed status for a pipeline run.

    Checks Redis first (fast path for active runs), then falls back to
    Elasticsearch for completed/expired runs.

    Args:
        pipeline_run_id: UUID of the pipeline run.
    """
    r = _get_redis()
    run = _redis_get_run(pipeline_run_id, r)

    if not run:
        es = _get_es()
        if es:
            try:
                resp = es.search(
                    index="pipeline-runs-*",
                    body={
                        "query": {"term": {"id": pipeline_run_id}},
                        "size": 1,
                    },
                )
                hits = resp.get("hits", {}).get("hits", [])
                if hits:
                    run = hits[0]["_source"]
            except Exception as exc:
                log.warning(
                    "Elasticsearch lookup failed for run %s: %s",
                    pipeline_run_id, exc
                )

    if not run:
        raise ValueError(
            f"Pipeline run '{pipeline_run_id}' not found in Redis or Elasticsearch."
        )

    return {
        "pipeline_run_id": run.get("id"),
        "project": run.get("project"),
        "board": run.get("board"),
        "issue_number": run.get("issue_number"),
        "issue_title": run.get("issue_title"),
        "issue_url": run.get("issue_url"),
        "status": run.get("status"),
        "outcome": run.get("outcome"),
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        "elapsed": _elapsed(run.get("started_at"), run.get("ended_at")),
        "discussion_id": run.get("discussion_id"),
    }


@mcp.tool()
async def analyze_run(pipeline_run_id: str) -> dict:
    """
    Trigger analysis for a pipeline run and return the diagnostic summary.

    POSTs to the existing /api/pipeline-run/<id>/analyze endpoint on the
    observability server (:5001), waits briefly, then fetches the result from
    /api/pipeline-run/<id>/analysis.

    Args:
        pipeline_run_id: UUID of the pipeline run to analyze.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        trigger = await client.post(
            f"{OBSERVABILITY_URL}/api/pipeline-run/{pipeline_run_id}/analyze"
        )
        trigger.raise_for_status()
        trigger_body = trigger.json()

        if not trigger_body.get("success"):
            return {
                "success": False,
                "pipeline_run_id": pipeline_run_id,
                "error": trigger_body.get("error", "Trigger returned success=false"),
            }

        # Brief pause to allow the async analysis task to complete.
        await asyncio.sleep(3)

        analysis = await client.get(
            f"{OBSERVABILITY_URL}/api/pipeline-run/{pipeline_run_id}/analysis"
        )
        analysis.raise_for_status()
        analysis_body = analysis.json()

    return {
        "success": True,
        "pipeline_run_id": pipeline_run_id,
        "triggered": True,
        "analysis": analysis_body.get("analysis"),
    }


@mcp.tool()
async def list_recent_runs(
    project: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Return recent pipeline runs (completed or failed), most recent first.

    Queries Elasticsearch pipeline-runs-* indices.

    Args:
        project: If given, filter to this project name.
        limit:   Maximum number of runs to return (default 10, max 50).
    """
    es = _get_es()
    if not es:
        raise RuntimeError(
            "Elasticsearch is not available — cannot query historical runs."
        )

    limit = min(limit, 50)
    es_query: dict[str, Any] = (
        {"term": {"project": project}} if project else {"match_all": {}}
    )

    try:
        resp = es.search(
            index="pipeline-runs-*",
            body={
                "query": es_query,
                "sort": [{"started_at": {"order": "desc"}}],
                "size": limit,
                "_source": [
                    "id", "project", "board",
                    "issue_number", "issue_title",
                    "started_at", "ended_at",
                    "status", "outcome",
                ],
            },
        )
    except Exception as exc:
        raise RuntimeError(f"Elasticsearch query failed: {exc}") from exc

    hits = resp.get("hits", {}).get("hits", [])
    return [
        {
            "pipeline_run_id": src.get("id"),
            "project": src.get("project"),
            "board": src.get("board"),
            "issue_number": src.get("issue_number"),
            "issue_title": src.get("issue_title"),
            "status": src.get("status"),
            "outcome": src.get("outcome"),
            "started_at": src.get("started_at"),
            "ended_at": src.get("ended_at"),
            "elapsed": _elapsed(src.get("started_at"), src.get("ended_at")),
        }
        for hit in hits
        for src in [hit["_source"]]
    ]


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5002))
    log.info("Starting switchyard-mcp on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
