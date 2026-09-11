# GitHub authentication

Switchyard supports two GitHub authentication methods: a Personal Access Token (PAT) and a GitHub App. Either can be used alone, or both together. `GITHUB_CREDENTIAL_PREFERENCE` selects which one `GitHubAPIClient` routes its own calls through (`pat` by default), with fallback to the other when the preferred one is unavailable.

Running **App-only** is supported, and is the right configuration for an organization that restricts personal access tokens. It requires an organization-owned app — see "Account type determines Projects v2 access".

## Why multiple authentication methods

A PAT is the simplest way to authenticate and covers most GitHub API operations — issues, pull requests, code, and comments. It requires no setup beyond creating a token and setting an environment variable.

Historically this codebase treated Discussions writes as App-only, on the grounds that no PAT scope grants them. **That is not correct against the current API.** Both credentials were verified with real writes against a throwaway repository:

| Mutation | PAT | GitHub App |
|---|---|---|
| `createDiscussion` | pass | pass |
| `addDiscussionComment` | pass | pass |

The PAT's discussion was authored by the token owner; the App's by the app's bot identity. Both were then deleted.

The PAT used carries `repo` **and** `write:discussion`, so this establishes that such a token works — not which of the two scopes is load-bearing. `write:discussion` is documented as covering *team* discussions rather than repository Discussions, which points at `repo`, but that has not been isolated. A deployment provisioning a minimal token should grant both until it has tested otherwise.

The GitHub App is still preferred for Discussions — bot identity and a separate rate-limit budget — but it is not required.

`services/github_discussions.py` uses `self.app.graphql_request` when the App is configured, and `_execute_graphql` falls back to a PAT-authenticated GraphQL call when it is not. That fallback works for writes as well as reads.

Beyond Discussions, the GitHub App provides two additional benefits: actions appear as `orchestrator-bot[bot]` rather than as the PAT owner's personal account, and the rate limit is per-installation (5,000 requests per hour for this installation) rather than shared across all applications using the same user token.

Supporting both lets operators start with a PAT for initial setup and add the GitHub App later, or run App-only from the start. Note that the App's advantages are real but bounded: bot identity, a separate rate-limit budget, and short-lived credentials rather than a long-lived one.

## Personal Access Token

### Required scopes

The `.env.example` specifies the following scopes for `GITHUB_TOKEN`:

- `repo` — full repository access (read/write to code, issues, PRs, comments)
- `project` — read/write access to GitHub Projects v2 boards
- `admin:repo_hook` — create and manage webhooks

The `project` scope is essential, and its absence fails **silently**: a Projects v2 query made without it returns an empty, successful result rather than an error, which board reconciliation would read as "no board exists" before creating a duplicate of every board. `services/github_capabilities.py` therefore checks the credential's grant (`x-oauth-scopes` for a PAT, installation permissions for an app) at startup, and `services/github_project_manager.py` skips reconciliation entirely rather than run it blind.

### Environment variable

```
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

### What it enables

With a PAT configured and no GitHub App, all API operations proceed through the GitHub CLI (`gh`) and the `requests` library using this token. The `github_api_client.py` `http_request` method checks `GH_TOKEN` first, then `GITHUB_TOKEN`, when constructing the `Authorization` header for direct HTTP calls. The GitHub CLI reads these same environment variables automatically.

### Limitations

- All comments and actions appear as the token owner's user account
- Rate limit is shared across all applications using the same token
- PAT tokens do not expire by default but can be revoked at any time; revocation immediately breaks all orchestrator operations
- Discussions writes DO work from a PAT (verified — see "Why multiple authentication methods" above); the App is still preferred for bot identity and a separate quota

## GitHub App

### What it provides

A GitHub App authenticates with a short-lived installation token rather than a long-lived credential. Each token is valid for one hour and is scoped to a specific installation of the app on a specific organization or user account. The bot identity (`orchestrator-bot[bot]`) appears in the GitHub UI as a distinct actor separate from any human user.

### Required environment variables

```
GITHUB_APP_ID=                         # Numeric app ID from the app's settings page
GITHUB_APP_INSTALLATION_ID=            # Numeric installation ID from the installed app
GITHUB_APP_PRIVATE_KEY_PATH=/path/to/orchestrator-bot.pem  # Path to the RSA private key file
```

The private key can alternatively be provided inline:

```
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----<br>...<br>-----END RSA PRIVATE KEY-----"
```

In production, `GITHUB_APP_PRIVATE_KEY_PATH` pointing to a file at `~/.orchestrator/<app-name>.pem` is preferred. The Docker Compose configuration mounts `~/.orchestrator` into the container at `/home/orchestrator/.orchestrator`.

`GitHubAppAuth` in `github_app_auth.py` checks `GITHUB_APP_PRIVATE_KEY_PATH` first; if that variable is unset, it falls back to `GITHUB_APP_PRIVATE_KEY`. The app is considered unconfigured if any of `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, or the private key is missing.

### How it works technically

GitHub App authentication is a two-step process.

**Step 1: Generate a JWT.** The orchestrator signs a JWT using the app's RSA private key. The JWT payload contains:

- `iat`: current Unix timestamp **minus 60 seconds**
- `exp`: current timestamp plus 540 seconds
- `iss`: the numeric app ID

`iat` is backdated so clock drift between the host and GitHub cannot fail the `iat` claim check, which surfaces as a 401 on the token exchange reported only as "Failed to get installation token". GitHub caps a JWT's lifetime at 10 minutes measured from `iat`, so `exp` is set to +540 to keep `exp - iat` at exactly 600 seconds.

The JWT is signed with the `RS256` algorithm. This JWT authenticates the app itself, not the installation.

**Step 2: Exchange the JWT for an installation token.** Using the JWT as a bearer token, the orchestrator calls:

```
POST https://api.github.com/app/installations/{installation_id}/access_tokens
```

GitHub returns a short-lived installation token and an `expires_at` timestamp. The token is cached in memory. Before the cached token is used, the code checks whether `datetime.now(timezone.utc) < token_expires_at`. A 5-minute safety margin is subtracted from the expiry so the token is refreshed before GitHub rejects it.

`GitHubApp` in `github_app.py` and `GitHubAppAuth` in `github_app_auth.py` implement this flow independently. `GitHubApp` is the global singleton used by `github_app.graphql_request` and `github_app.rest_request`. `GitHubAppAuth` is a separate module used by other consumers that need an installation token directly.

### Token refresh on 401

Both `graphql_request` and `rest_request` in `github_app.py` implement an automatic refresh-and-retry on HTTP 401 responses. The sequence is:

1. Initial request fails with 401.
2. `_get_token(force_refresh=True)` is called, which calls `_invalidate_token()` and then `get_installation_token()` to fetch a new token.
3. The request is retried once with the new token.
4. If the retry also returns 401, `GITHUB_TOKEN` (the PAT) is used as a final fallback for that specific request.

This handles the case where a cached token has been revoked or has expired without the in-process clock noticing.

## Runtime auth selection

### In `github_app.py`

The `_get_token` method determines which credential to use for each request:

1. If the GitHub App is enabled (`self.enabled` is True), call `get_installation_token()`.
2. If that returns a valid token, use it.
3. If the app is not enabled or token generation fails, return `os.environ.get('GITHUB_TOKEN')`.

This means a PAT in `GITHUB_TOKEN` functions as a fallback for any call made through `github_app.graphql_request` or `github_app.rest_request`.

### In `github_api_client.py`

`GitHubAPIClient` does not use `GitHubApp` directly. Its three execution paths — `graphql`, `rest`, and `gh_cli` — invoke the GitHub CLI (`gh api graphql`, `gh api`, arbitrary `gh` commands). The CLI inherits authentication from the environment: it reads `GH_TOKEN` then `GITHUB_TOKEN`, and if `gh auth login` has been run, it uses the stored credential. The `http_request` method makes direct HTTP calls and applies the same env-var lookup (`GH_TOKEN` or `GITHUB_TOKEN`) when building the `Authorization` header.

`GitHubAPIClient` resolves its credential per call rather than reading a static environment variable. The `GITHUB_CREDENTIAL_PREFERENCE` environment variable selects which:

- `pat` (default) — read `GH_TOKEN`/`GITHUB_TOKEN` from the environment, the historical behaviour
- `app` — mint a GitHub App installation token per call via `services/github_app.py`

An `app` preference still falls back to the PAT when the App is not configured or its token exchange fails, so a partially configured deployment degrades rather than going dark. The fallback is reported, not silent, because the credential a call actually used selects which rate-limit bucket its response headers update (see below).

Setting `GITHUB_TOKEN` to an installation token manually does not work: installation tokens expire after one hour, and a static environment variable cannot rotate.

## The `GitHubAPIClient`: rate limit tracking and circuit breaker

`GitHubAPIClient` (`services/github_api_client.py`) is the centralized gateway for all API interactions that need rate limit awareness. It wraps the GitHub CLI and direct HTTP calls with two cross-cutting behaviors.

### Credential-scoped rate limit buckets

A rate-limit reading belongs to the *(credential, resource)* pair whose budget was actually spent, never to the transport that produced it. `GitHubAPIClient` therefore keeps four buckets — PAT/GraphQL, PAT/REST, App/GraphQL, App/REST — and selects among them using the credential a call ended up on, including when an App-preference call fell back to the PAT.

App readings mirror to their own Redis keys (`github:rate_limit:{rest,graphql}:app`). The two original keys keep meaning exactly "the PAT budget", so cross-process readers are unaffected; on an App-only deployment they correctly go stale rather than reporting a budget nothing is spending.

### Rate limit tracking

`GitHubRateLimitStatus` tracks the current limit, remaining quota, and reset time. It is updated from two sources:

- HTTP response headers (`x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-reset`, `x-ratelimit-resource`) after each `http_request` call
- The `extensions.cost.rateLimit` or `data.rateLimit` fields in GraphQL responses

On startup, a background daemon thread waits 5 seconds and then queries the GraphQL `rateLimit` field to populate accurate values rather than starting with defaults. A second background thread repeats this query every 300 seconds.

When usage exceeds thresholds, the client inserts blocking sleeps before executing requests:

| Usage threshold | Action |
|---|---|
| Above 80% | Log warning, no delay |
| Above 90% | Sleep 10 seconds before request |
| Above 95% | Sleep 30 seconds before request |
| Limit hit | Trip circuit breaker |

An alarm is logged at critical level when fewer than 100 points remain, error level below 250, warning above 90%, and info above 80%.

### Circuit breaker

`GitHubBreaker` implements a three-state circuit breaker (`CLOSED`, `OPEN`, `HALF_OPEN`) that protects the system when the rate limit is exhausted.

When a rate limit error is detected in any response — GraphQL, REST, or CLI — `breaker.trip(reset_time)` is called. This opens the breaker and records a reset time, defaulting to one hour from now if the API response does not include a specific reset timestamp. Breaker state is persisted to Redis under the key `orchestrator:github_api_breaker:state` so the state survives orchestrator restarts within a rate limit window.

While the breaker is open, every call through `GitHubAPIClient` returns immediately with:

```python
(False, {"error": "GitHub API rate limit exceeded - circuit breaker open"})
```

At reset time, the breaker transitions to `HALF_OPEN`. The next successful request closes it back to `CLOSED`. The breaker state is available through the observability API:

```bash
curl http://localhost:5001/api/circuit-breakers
```

### Retry behavior

Transient errors (non-rate-limit failures) are retried up to three times with exponential backoff: 2 seconds, 4 seconds, 8 seconds. Client errors (HTTP 401, 403, 404, 410, 422) are not retried. A 410 response indicates the resource has been permanently deleted and is returned immediately as `{"error": "resource_deleted", "http_code": 410}`.

## Setting up a GitHub App

The following steps create and configure the GitHub App that the orchestrator uses.

### 1. Create the app

**Who should own the app.** If the app needs to manage Projects v2 boards, it must be owned by and installed on an **organization**:

```
https://github.com/organizations/<org>/settings/apps/new
```

An app owned by a personal account cannot reach Projects v2 at all — see "Account type determines Projects v2 access" below. App creation lives under the org's **Settings → Developer settings → GitHub Apps → New GitHub App**, which is a different page from the org's "GitHub Apps" installed-apps list (the one showing Marketplace / My apps). An app created on a personal account by mistake can be moved with **Transfer ownership** at the bottom of its settings page, which preserves the app ID and existing private keys.

For a deployment that does not manage boards, a personal-account app (`https://github.com/settings/apps/new`) is sufficient.

Set:
- **App name**: a unique name, e.g., `orchestrator-bot`
- **Homepage URL**: any valid URL
- **Webhook**: disable (uncheck "Active") unless you intend to use webhook delivery
- **Where can this GitHub App be installed?**: "Only on this account"

Note that "Only on this account" means the owning account. On a personal-account app it therefore excludes every organization, and the org will not appear as an option at install time.

### 2. Configure permissions

Under "Repository permissions", set:

| Permission | Level | Needed for |
|---|---|---|
| Contents | Read and write | repository access; also required to *link* a new board to a repository |
| Issues | Read and write | issue comments, labels, board cards |
| Pull requests | Read and write | PR creation and the review cycle |
| Discussions | Read and write | the `planning_design` pipeline (`workspace: discussions`) |
| Metadata | Read-only | mandatory for every app |

Under "Organization permissions":

| Permission | Level | Needed for |
|---|---|---|
| Projects | Read and write | **Projects v2 boards** |

**Do not confuse the two "Projects" permissions.** Both appear in the UI under that single word, in separate sections of the same page:

- **Repository permissions → Projects** is labelled "Manage classic projects within a repository". It governs *classic* project boards and grants no Projects v2 access whatsoever. Granting it in place of the organization permission is a silent no-op for this orchestrator.
- **Organization permissions → Projects** is the one that governs Projects v2, and the one board reconciliation requires.

No event subscriptions are required unless the app will receive webhooks.

Changing an app's permissions after installation does not apply them retroactively: the installation keeps its previously accepted set until the owning account approves the new one (a banner appears at `https://github.com/settings/installations`). The two can be compared directly — `GET /app` reports what the registration *requests*, `GET /app/installations/{id}` what the installation has *accepted*. If those disagree, an approval is pending; if the registration itself lacks the permission, the change was never saved.

### Account type determines Projects v2 access

A GitHub App can only reach Projects v2 owned by an **organization**. There is no equivalent permission for boards owned by a personal account, and an app installed on a personal account does not receive that account's `projectsV2` even for boards linked to repositories the app can otherwise access.

This is the practical constraint on running without a PAT: board management requires an org-owned app installed on the org that owns the boards. Everything else the orchestrator does — issues, pull requests, discussions, repository contents — works from a personal-account app.

### Verified configuration

The six permissions above were verified end to end against an organization-owned app installed on an organization, reading a **private** Projects v2 board:

| Check | Result |
|---|---|
| Token carries `organization_projects: write` | pass |
| App sees the private board exists | pass |
| App reads the board's **items** | pass |
| App resolves item content inside a private repository | pass |
| App creates a board (what reconciliation does) | pass |
| App reads the Status field's options (column configuration) | pass |
| App creates a discussion and comments on it | pass |

`gh project list --owner <org>` through `GitHubAPIClient.gh_cli()` returns the org's boards on this configuration. The same command returns an empty, successful result — no error — when the Projects permission is absent, which is the failure the reconciliation guard in `services/github_project_manager.py` exists to catch.

Note that `Workflows` is not in the list. It is not required by anything the orchestrator does, and omitting it keeps the permission request smaller.

With these six permissions an organization-owned app covers every GitHub operation the orchestrator performs, including Discussions — so **App-only needs no PAT at all**. The organization's default discussion categories include `Ideas`, which is what `config/foundations/pipelines.yaml` sets as the `planning_design` pipeline's `discussion_category`; no extra setup is needed for that pipeline.

### 3. Generate a private key

After saving the app, scroll to the "Private keys" section and click "Generate a private key". Download the `.pem` file. Store it at `~/.orchestrator/<app-name>.pem`.

### 4. Record the app ID

The app ID is shown at the top of the app settings page as a numeric value, e.g., `12345`.

### 5. Install the app

Navigate to the app's settings page and click "Install App". Select the organization or account and choose which repositories the app can access. After installation, the URL will contain the installation ID: `https://github.com/settings/installations/<installation_id>`.

The installation ID can also be recovered from the app's own credentials without visiting the UI — `GET /app/installations`, authenticated with the app JWT, lists every installation with its ID, account and accepted permissions.

### 6. Configure environment variables

```bash
GITHUB_APP_ID=12345
GITHUB_APP_INSTALLATION_ID=67890
GITHUB_APP_PRIVATE_KEY_PATH=/home/orchestrator/.orchestrator/orchestrator-bot.pem
```

Restart the orchestrator after setting these variables.

### 7. Verify the configuration

```bash
python scripts/test_github_app.py
```

A successful run prints the generated installation token prefix and expiry, confirming the full JWT-to-token flow works. If `GITHUB_ORG` and `GITHUB_REPO` are set, it also tests the Discussions API.

## Common errors and diagnostics

### "GitHub App authentication not configured, will use PAT fallback"

Logged at warning level by `GitHubAppAuth.__init__` when any of `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, or the private key is missing. The orchestrator continues using `GITHUB_TOKEN`.

Check which variable is missing:

```bash
echo $GITHUB_APP_ID
echo $GITHUB_APP_INSTALLATION_ID
echo $GITHUB_APP_PRIVATE_KEY_PATH
```

### "Failed to load private key file"

`GITHUB_APP_PRIVATE_KEY_PATH` is set but the file does not exist at that path inside the container. Verify the host path is correct and that the `~/.orchestrator` directory is mounted. Check `docker-compose.yml` for the volume mount:

```
~/.orchestrator:/home/orchestrator/.orchestrator
```

Inside the container, the path should be `/home/orchestrator/.orchestrator/<app-name>.pem`.

### "Failed to get installation token" with HTTP 401

The JWT is being rejected by GitHub. Causes:

- The app ID (`GITHUB_APP_ID`) does not match the private key
- The private key has been revoked; generate a new one in the app settings
- System clock skew; the JWT `iat` must be within a few minutes of GitHub's time

### "Failed to get installation token" with HTTP 403

The app does not have permission to access the installation. Verify:

- `GITHUB_APP_INSTALLATION_ID` matches an actual installation visible at `https://github.com/settings/installations`
- The app is installed on the correct organization or account

### GraphQL or REST returns HTTP 401 after a period of operation

The cached installation token expired and the refresh failed. The `graphql_request` and `rest_request` methods will attempt one automatic refresh and retry. If both attempts fail, the PAT fallback is tried. If that also fails, check whether `GITHUB_TOKEN` is still valid:

```bash
gh auth status
```

### Circuit breaker is open

The rate limit was exhausted. The breaker will not close until the reset time. Check the current state and reset time:

```bash
curl http://localhost:5001/api/circuit-breakers
```

To determine why the limit was hit, review the call trace logs. The orchestrator logs a summary every 5 minutes under the prefix `GitHub API Call Summary (last hour):` showing the top callers by volume. If the rate limit is regularly exhausted, configuring a GitHub App (if not already done) will not help — the App rate limit is per-installation, not shared, but still bounded at 5,000 requests per hour. Reduce polling frequency in `services/project_monitor.py` or optimize GraphQL queries to request less data per call.

### No GitHub token found in environment variables

Logged by `http_request` when neither `GH_TOKEN` nor `GITHUB_TOKEN` is set. The request proceeds without authentication (GitHub public API, rate limited to 60 requests per hour). Set `GITHUB_TOKEN` to a valid PAT and restart.
