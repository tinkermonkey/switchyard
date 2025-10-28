# MCP Servers Guide for Orchestrator

## Overview

This guide lists recommended Model Context Protocol (MCP) servers that work with the orchestrator **without requiring Docker socket access** to agent containers.

## Security Principle

**Agent containers should NOT have Docker socket access.** This maintains security isolation and prevents agents from spawning arbitrary containers.

## Recommended MCP Servers

### ✅ Currently Configured

#### 1. **Context7** (HTTP)
```yaml
type: http
url: "${CONTEXT7_MCP_URL}"
```
- **Transport**: HTTP
- **Requires**: API key in environment
- **Capabilities**: Library documentation, API references, package search
- **Status**: ✅ Configured, requires `CONTEXT7_API_KEY` and `CONTEXT7_MCP_URL` in `.env`

#### 2. **Serena** (Python/stdio)
```yaml
type: stdio
command: uvx
args:
  - --from
  - git+https://github.com/oraios/serena
  - serena
  - start-mcp-server
```
- **Transport**: stdio (no Docker needed)
- **Requires**: `uvx` installed in agent container (Python package runner)
- **Capabilities**: Codebase analysis, semantic search, code understanding, symbol lookup
- **Status**: ✅ Configured

---

### 🆕 Recommended Additions

#### 3. **Fetch MCP Server** (Node.js/stdio) - **RECOMMENDED**
```yaml
fetch:
  type: stdio
  command: npx
  args:
    - -y
    - "@modelcontextprotocol/server-fetch"
  capabilities:
    - web_content_fetching
    - html_to_markdown
    - webpage_analysis
  description: "Fetch and convert web content to various formats (HTML, JSON, Markdown)"
```

**Installation**: None needed - `npx -y` auto-installs on first use
**Perfect for**: Web scraping, documentation fetching, content extraction
**No Docker required**: Pure Node.js, runs via npx

#### 4. **GitHub MCP Server** (Node.js/stdio)
```yaml
github:
  type: stdio
  command: npx
  args:
    - -y
    - "@modelcontextprotocol/server-github"
  env:
    GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
  capabilities:
    - repository_access
    - file_operations
    - issue_management
    - pr_management
  description: "Interact with GitHub repositories, issues, and PRs"
```

**Installation**: None needed - `npx -y` auto-installs
**Perfect for**: Repository analysis, code review, issue tracking
**Uses existing**: `GITHUB_TOKEN` from orchestrator

#### 5. **Filesystem MCP Server** (Node.js/stdio)
```yaml
filesystem:
  type: stdio
  command: npx
  args:
    - -y
    - "@modelcontextprotocol/server-filesystem"
    - "{work_dir}"
  capabilities:
    - file_reading
    - file_writing
    - directory_listing
    - file_search
  description: "Secure file operations with configurable access controls"
```

**Installation**: None needed
**Perfect for**: Enhanced file operations beyond basic Read/Write tools
**Security**: Scoped to specific directories

#### 6. **Brave Search MCP** (Node.js/stdio)
```yaml
brave-search:
  type: stdio
  command: npx
  args:
    - -y
    - "@modelcontextprotocol/server-brave-search"
  env:
    BRAVE_API_KEY: "${BRAVE_API_KEY}"
  capabilities:
    - web_search
    - real_time_information
  description: "Web search using Brave Search API"
```

**Installation**: None needed
**Perfect for**: Real-time web search (alternative to WebSearch tool)
**Requires**: Brave API key (free tier available)

#### 7. **Firecrawl MCP** (Node.js/stdio)
```yaml
firecrawl:
  type: stdio
  command: npx
  args:
    - -y
    - firecrawl-mcp-server
  env:
    FIRECRAWL_API_KEY: "${FIRECRAWL_API_KEY}"
  capabilities:
    - advanced_web_scraping
    - sitemap_crawling
    - content_extraction
  description: "Powerful web scraping using Firecrawl API"
```

**Installation**: None needed
**Perfect for**: Advanced web scraping needs
**Requires**: Firecrawl API key

---

## MCP Servers to AVOID

### ❌ **Puppeteer MCP** (Docker-based)
- **Reason**: Requires Docker socket access or spawning Docker containers
- **Alternative**: Use `fetch` MCP server for basic web scraping, or Firecrawl for advanced needs

### ❌ **Any Docker-based stdio MCP servers**
- **Reason**: Would require agents to have Docker socket access
- **Pattern**: If config has `command: docker` + `args: [run...]`, avoid it

---

## How to Add a New MCP Server

### 1. Add to `config/foundations/mcp.yaml`
```yaml
mcp_servers:
  my-server:
    type: stdio  # or http
    command: npx
    args:
      - -y
      - "@modelcontextprotocol/server-name"
    capabilities:
      - capability1
      - capability2
    description: "What this server does"
```

### 2. Add to Agent Config (`config/foundations/agents.yaml`)
```yaml
agents:
  senior_software_engineer:
    # ... other config ...
    mcp_servers:
      - context7
      - my-server  # Add your server name here
```

### 3. Test with Test Script
```bash
docker compose exec orchestrator python test_puppeteer_mcp.py
```

---

## MCP Server Transport Types

### **stdio** (Process-based)
- Agent spawns server as child process
- Communicates via stdin/stdout
- Examples: `npx`, `uvx`, Python scripts
- **Safe**: No Docker needed

### **http** (Network-based)
- Agent makes HTTP requests
- Server runs independently (could be external service or docker-compose service)
- Examples: Context7, Firecrawl API
- **Safe**: No Docker socket needed

### **Docker-based stdio** (NOT RECOMMENDED)
- Uses `docker run` or `docker exec` commands
- Requires Docker socket access
- **Avoid**: Security risk

---

## Best Practices

1. **Prefer npx-based servers**: Auto-install, no dependency management needed
2. **Use HTTP servers for external services**: Clean separation, no process management
3. **Never give agents Docker socket access**: Maintain security boundaries
4. **Test incrementally**: Add one MCP server at a time and verify it works
5. **Document API keys needed**: Add to `.env.example` with comments

---

## Resources

- [Official MCP Servers Repository](https://github.com/modelcontextprotocol/servers)
- [MCP Specification](https://spec.modelcontextprotocol.io/)
- [Awesome MCP Servers](https://mcpservers.org/)
- [MCP Documentation](https://www.anthropic.com/news/model-context-protocol)

---

## Next Steps

**Immediate (No Setup Required)**:
1. Add `fetch` MCP server for web scraping
2. Add `github` MCP server (already have token)
3. Test with senior_software_engineer agent

**Later (Requires API Keys)**:
1. Set up Brave Search API key for enhanced search
2. Consider Firecrawl for advanced web scraping
3. Explore database MCP servers (PostgreSQL, MongoDB) if needed
