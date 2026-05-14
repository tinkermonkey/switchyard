---
name: github-issue-query
description: Reference for correctly querying GitHub issue state - board status, parent/child links, sub-issues. Use these patterns instead of `gh issue view --json projectItems` which does not reliably return status field values.
user_invocable: false
---

# GitHub Issue Query Reference

The orchestrator uses GitHub Projects v2 GraphQL API. `gh issue view --json projectItems` is unreliable for board status because it does not apply the inline fragment selection needed to extract `ProjectV2ItemFieldSingleSelectValue` nodes. Always use explicit `gh api graphql` calls.

## Project Numbers (context-studio)

From `state/projects/context-studio/github_state.yaml`:

| Board | Project Number |
|---|---|
| Planning & Design | 11 |
| SDLC Execution | 12 |

Owner: `tinkermonkey` (user, not org — queries use `user(login:)` not `organization(login:)`)

---

## 1. Board status for a single issue

Uses the same pattern as `services/project_monitor.py::get_project_items` via `test_query_issue_project_status` in the integration tests. Queries by issue, traverses `projectItems → fieldValues`.

```bash
ISSUE=865
gh api graphql -f query='{
  repository(owner: "tinkermonkey", name: "context-studio") {
    issue(number: '"$ISSUE"') {
      number
      title
      state
      projectItems(first: 10) {
        nodes {
          project { number title }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field {
                  ... on ProjectV2SingleSelectField { name }
                }
              }
            }
          }
        }
      }
    }
  }
}' | jq '.data.repository.issue | {
  number, title, state,
  boards: [.projectItems.nodes[] | {
    board: .project.title,
    status: (.fieldValues.nodes[] | select(.field.name? == "Status") | .name)
  }]
}'
```

An empty `boards: []` array means the issue is **not on any board** — the orchestrator will never pick it up.

---

## 2. All items on a board (how the orchestrator polls)

Source: `services/github_owner_utils.py::build_projects_v2_query`. This is what `project_monitor.get_project_items()` actually runs. Query by project number, not by issue.

```bash
PROJECT=12   # SDLC Execution
gh api graphql -f query='{
  user(login: "tinkermonkey") {
    projectV2(number: '"$PROJECT"') {
      title
      items(first: 100, orderBy: {field: POSITION, direction: ASC}) {
        nodes {
          content {
            ... on Issue {
              number
              title
              state
            }
          }
          fieldValues(first: 10) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field {
                  ... on ProjectV2SingleSelectField { name }
                }
              }
            }
          }
        }
      }
    }
  }
}' | jq '.data.user.projectV2.items.nodes[]
  | select(.content != null)
  | {
      number: .content.number,
      title: .content.title,
      state: .content.state,
      status: (.fieldValues.nodes[] | select(.field.name? == "Status") | .name)
    }'
```

---

## 3. Parent issue (is this a sub-issue of something?)

Source: `services/feature_branch_manager.py::get_parent_issue`. Uses GitHub's native `parent` field on `Issue`.

```bash
ISSUE=865
gh api graphql -f query='{
  repository(owner: "tinkermonkey", name: "context-studio") {
    issue(number: '"$ISSUE"') {
      number
      title
      parent {
        ... on Issue { number title state }
      }
    }
  }
}' | jq '.data.repository.issue | {number, title, parent}'
```

`"parent": null` means top-level issue. A non-null result means this is a sub-issue.

---

## 4. Sub-issues (children of a parent)

Source: `services/feature_branch_manager.py::_get_sub_issues_from_parent`. Uses the `subIssues` field. Requires the `GraphQL-Features: sub_issues` header.

```bash
PARENT=842
gh api graphql \
  -H 'GraphQL-Features: sub_issues' \
  -f query='{
    repository(owner: "tinkermonkey", name: "context-studio") {
      issue(number: '"$PARENT"') {
        number
        title
        subIssues(first: 100) {
          totalCount
          nodes { number title state url }
        }
      }
    }
  }' | jq '.data.repository.issue | {
    number, title,
    child_count: .subIssues.totalCount,
    children: [.subIssues.nodes[] | {number, title, state}]
  }'
```

---

## 5. Link a child issue to a parent (addSubIssue)

Source: `pipeline/pr_review_stage.py::_link_sub_issue`. Get node IDs first, then mutate.

```bash
# Step 1: Get node IDs
PARENT=842; CHILD=865
gh api graphql -f query='{
  repository(owner: "tinkermonkey", name: "context-studio") {
    parent: issue(number: '"$PARENT"') { id }
    child:  issue(number: '"$CHILD"')  { id }
  }
}' | jq '.data.repository | {parent_id: .parent.id, child_id: .child.id}'

# Step 2: Link
PARENT_ID="<id from above>"
CHILD_ID="<id from above>"
gh api graphql \
  -H 'GraphQL-Features: sub_issues' \
  -f query='mutation {
    addSubIssue(input: {
      issueId: "'"$PARENT_ID"'",
      subIssueId: "'"$CHILD_ID"'"
    }) {
      issue    { number title }
      subIssue { number title }
    }
  }'
```

---

## 6. Combined: issue state + board + parent + children

Full diagnostic snapshot for a single issue:

```bash
ISSUE=842
gh api graphql \
  -H 'GraphQL-Features: sub_issues' \
  -f query='{
    repository(owner: "tinkermonkey", name: "context-studio") {
      issue(number: '"$ISSUE"') {
        number title state
        parent { ... on Issue { number title } }
        subIssues(first: 100) {
          totalCount
          nodes { number title state }
        }
        projectItems(first: 10) {
          nodes {
            project { number title }
            fieldValues(first: 20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  name
                  field { ... on ProjectV2SingleSelectField { name } }
                }
              }
            }
          }
        }
      }
    }
  }' | jq '.data.repository.issue | {
    number, title, state,
    parent,
    children: [.subIssues.nodes[] | {number, title, state}],
    boards: [.projectItems.nodes[] | {
      board: .project.title,
      status: (.fieldValues.nodes[] | select(.field.name? == "Status") | .name)
    }]
  }'
```

---

## Common mistakes

| Wrong | Why | Right |
|---|---|---|
| `gh issue view NNN --json projectItems \| jq '.projectItems[].fieldValues...'` | `fieldValues` nodes returned by `gh issue view` don't resolve `ProjectV2ItemFieldSingleSelectValue` fragments reliably | Use `gh api graphql` with explicit inline fragments (query 1 above) |
| `gh issue view NNN --json projectItems \| jq '.projectItems[].fieldValues.nodes[].field.name'` | Most `fieldValues` nodes are null; only `SingleSelectValue` nodes have `field.name` | Filter with `select(.field.name? == "Status")` |
| Checking `subIssues` without `-H 'GraphQL-Features: sub_issues'` | The field is behind a feature flag and returns null without the header | Always include the header for `subIssues` and `addSubIssue` |
| Querying board status directly via issue `projectItems` to check if it's on a board | Doesn't tell you what the orchestrator sees — the monitor queries the board, not the issue | Use query 2 (board items list) to see exactly what the orchestrator's poll returns |
