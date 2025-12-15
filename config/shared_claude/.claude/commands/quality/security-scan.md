---
description: Scan for security vulnerabilities and issues
allowed-tools: Bash(bandit:*), Bash(safety:*), Bash(npm:*), Bash(pip:*), Bash(git:*), Bash(find:*), Bash(grep:*)
argument-hint: [scope]
---

# Security Vulnerability Scan

## Dependency Files

!`ls -la | grep -E "(requirements\.txt|package\.json|poetry\.lock|Pipfile\.lock|package-lock\.json)"`

## Git Secrets Check

!`git log --all --pretty=format: --name-only --diff-filter=A | sort -u | grep -E "\.(env|key|pem|cert)$" | head -20`

## Task

Run security scan for: $ARGUMENTS

**Instructions:**

1. **Scan for common vulnerabilities:**

**Python security:**
- **bandit** (code scan): `bandit -r . -f json`
- **safety** (dependencies): `safety check --json`
- **pip-audit**: `pip-audit`

**Node.js security:**
- **npm audit**: `npm audit --json`
- **npm audit fix**: `npm audit fix` (with user permission)

2. **Check for sensitive data:**
```bash
# Look for potential secrets
grep -r -E "(password|secret|api[_-]?key|token)" . --exclude-dir=node_modules --exclude-dir=.git
```

3. **File permission checks:**
```bash
# Check for overly permissive files
find . -type f -perm /go+w | grep -v ".git"
```

4. **Dependency vulnerabilities:**
   - List all vulnerable dependencies
   - Severity levels (critical, high, moderate, low)
   - Suggested fixes or updates

5. **Report findings:**
   - **Critical**: Immediate action required
   - **High**: Should fix soon
   - **Medium**: Fix when possible
   - **Low/Info**: Good to know

**Scan scopes:**
- `code`: Scan source code for vulnerabilities
- `dependencies`: Check dependency vulnerabilities
- `secrets`: Look for accidentally committed secrets
- `all` (default): Comprehensive scan

**Output:**
- Summary of findings by severity
- Detailed vulnerability descriptions
- Remediation steps
- Links to CVE databases

IMPORTANT: Never output actual secrets or credentials in results. Redact sensitive data.
