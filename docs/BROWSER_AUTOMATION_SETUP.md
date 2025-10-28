# Browser Automation Setup for UX Testing

## Problem Statement

Agents need browser automation for UX workflow testing, but **cannot have Docker socket access** for security reasons.

## Solution Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Docker Compose Services                                      │
│                                                              │
│  ┌────────────────────────────┐                            │
│  │ browserless/chrome          │                            │
│  │ - Headless Chrome           │                            │
│  │ - Exposes WebSocket API     │                            │
│  │ - Port 3000                 │                            │
│  │ - No Docker socket needed   │                            │
│  └────────────────────────────┘                            │
│           ↑                                                  │
│           │ WebSocket/HTTP                                  │
│           │                                                  │
│  ┌────────────────────────────────────────┐                │
│  │ Agent Container                         │                │
│  │                                          │                │
│  │  Claude Code spawns:                    │                │
│  │  npx playwright-mcp                     │                │
│  │    ↓                                    │                │
│  │  Connects to: ws://browserless:3000    │                │
│  │                                          │                │
│  │  ✅ No Docker socket needed             │                │
│  └────────────────────────────────────────┘                │
└─────────────────────────────────────────────────────────────┘
```

## Implementation Steps

### 1. Add Browserless to docker-compose.yml

```yaml
services:
  browserless:
    image: ghcr.io/browserless/chromium:latest
    container_name: browserless
    environment:
      - CONCURRENT=5
      - TOKEN=your-secure-token-here  # Optional: Add authentication
      - MAX_CONCURRENT_SESSIONS=5
      - TIMEOUT=30000
      - ENABLE_DEBUGGER=true
    networks:
      - orchestrator-net
    ports:
      - "3000:3000"  # Optional: Expose for debugging
    restart: unless-stopped
    # Resource limits to prevent Chrome from consuming too much memory
    deploy:
      resources:
        limits:
          memory: 2G
        reservations:
          memory: 512M
```

### 2. Add Playwright MCP to config/foundations/mcp.yaml

**Option A: Microsoft's Official Playwright MCP**
```yaml
mcp_servers:
  playwright:
    type: stdio
    command: npx
    args:
      - -y
      - "@microsoft/playwright-mcp"
    env:
      # Connect to browserless service
      PLAYWRIGHT_CHROMIUM_WS_ENDPOINT: "ws://browserless:3000"
    capabilities:
      - browser_automation
      - ui_testing
      - screenshot_capture
      - accessibility_testing
      - network_interception
    description: "Microsoft Playwright MCP for browser automation via remote Chrome"
```

**Option B: Playwright CDP MCP (with extra CDP features)**
```yaml
mcp_servers:
  playwright-cdp:
    type: stdio
    command: npx
    args:
      - -y
      - mcp-playwright-cdp
    env:
      CDP_ENDPOINT: "ws://browserless:3000"
    capabilities:
      - browser_automation
      - chrome_devtools
      - performance_analysis
      - network_debugging
    description: "Playwright with Chrome DevTools Protocol access for advanced debugging"
```

### 3. Add to Agent Configuration

```yaml
# config/foundations/agents.yaml
agents:
  senior_software_engineer:
    # ... existing config ...
    mcp_servers:
      - context7
      - playwright  # Add browser automation
```

## Browserless API Features

### WebSocket API (Puppeteer/Playwright)
```javascript
// Claude Code's Playwright MCP will use this automatically
const browser = await playwright.chromium.connectOverCDP('ws://browserless:3000');
```

### REST APIs (Alternative approach)
Browserless also provides REST endpoints:
- `POST /screenshot` - Capture screenshots
- `POST /pdf` - Generate PDFs
- `POST /content` - Get page content
- `POST /function` - Execute custom Puppeteer code

## Testing the Setup

### 1. Start Browserless
```bash
docker compose up -d browserless
```

### 2. Verify Browserless is Running
```bash
# Check health
curl http://localhost:3000/

# Test screenshot endpoint
curl -X POST http://localhost:3000/screenshot \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```

### 3. Test with Agent
Create a test task asking agent to:
- Navigate to a URL
- Take a screenshot
- Test a form submission
- Verify UI elements

## Example Agent Prompts

**Basic Navigation & Screenshot:**
```
Navigate to https://example.com and take a screenshot of the homepage.
Describe what you see.
```

**Form Testing:**
```
Go to https://example.com/contact
Fill out the contact form with test data
Submit the form
Verify the success message appears
```

**Accessibility Testing:**
```
Check https://example.com for accessibility issues:
- Missing alt text on images
- Form labels
- Color contrast
- Keyboard navigation
```

## Advantages of This Architecture

✅ **No Docker Socket Access**: Agents never need Docker permissions
✅ **Shared Resource**: One Chrome instance serves all agents
✅ **Scalable**: Browserless handles concurrency automatically
✅ **Reliable**: Production-grade browser automation
✅ **Feature-Rich**: Full Playwright/Puppeteer capabilities
✅ **Debuggable**: Can connect browser DevTools for debugging

## Resource Management

**Memory Usage:**
- Browserless Base: ~200MB
- Per Session: ~100-200MB
- With 5 concurrent sessions: ~1-1.5GB total

**Recommended Settings:**
```yaml
deploy:
  resources:
    limits:
      memory: 2G      # Max memory
      cpus: '2.0'     # Max CPU cores
    reservations:
      memory: 512M    # Min memory
```

## Security Considerations

### 1. Authentication (Recommended)
```yaml
environment:
  - TOKEN=your-secure-random-token
```

Then in MCP config:
```yaml
env:
  PLAYWRIGHT_CHROMIUM_WS_ENDPOINT: "ws://browserless:3000?token=your-secure-random-token"
```

### 2. Network Isolation
Browserless runs on `orchestrator-net` - only accessible to orchestrator containers.

### 3. Resource Limits
Set memory/CPU limits to prevent runaway Chrome processes.

## Troubleshooting

### Issue: Connection Refused
```bash
# Check browserless is running
docker compose ps browserless

# Check logs
docker compose logs browserless
```

### Issue: Out of Memory
```bash
# Increase memory limit in docker-compose.yml
deploy:
  resources:
    limits:
      memory: 4G  # Increase from 2G
```

### Issue: Session Timeout
```yaml
# Increase timeout in browserless config
environment:
  - TIMEOUT=60000  # 60 seconds
```

## Alternative: Browserless Cloud

If you don't want to self-host, Browserless offers a cloud service:
```yaml
playwright:
  env:
    PLAYWRIGHT_CHROMIUM_WS_ENDPOINT: "wss://chrome.browserless.io?token=YOUR_API_KEY"
```

**Pros**: No infrastructure management
**Cons**: Costs money, external dependency

## Next Steps

1. Add browserless service to docker-compose.yml
2. Choose Playwright MCP variant (Microsoft official recommended)
3. Add to mcp.yaml
4. Add to senior_software_engineer agent
5. Test with simple navigation task
6. Expand to complex UX workflows

## Resources

- [Browserless Documentation](https://docs.browserless.io/)
- [Browserless GitHub](https://github.com/browserless/browserless)
- [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [Playwright CDP MCP](https://github.com/lars-hagen/mcp-playwright-cdp)
