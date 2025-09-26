"""
MCP Server Integration for Claude Code Orchestrator

Provides lightweight integration with MCP servers for agents.
Handles connection management and tool routing.
"""

import os
import httpx
import json
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

@dataclass
class MCPServer:
    name: str
    url: str
    capabilities: List[str]

class MCPIntegration:
    def __init__(self, config: Dict[str, Any]):
        self.servers = {}
        self.client = httpx.AsyncClient(timeout=30.0)

        # Initialize MCP servers from config
        mcp_configs = config.get('mcp_servers', [])
        for server_config in mcp_configs:
            server = MCPServer(
                name=server_config['name'],
                url=os.path.expandvars(server_config['url']),
                capabilities=server_config.get('capabilities', [])
            )
            self.servers[server.name] = server

    async def get_available_tools(self, server_name: str) -> List[str]:
        """Get available tools from an MCP server"""
        if server_name not in self.servers:
            return []

        server = self.servers[server_name]
        try:
            response = await self.client.get(f"{server.url}/tools")
            if response.status_code == 200:
                tools = response.json().get('tools', [])
                return [tool['name'] for tool in tools]
        except Exception as e:
            print(f"Warning: Failed to get tools from {server_name}: {e}")

        return []

    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on an MCP server"""
        if server_name not in self.servers:
            raise ValueError(f"MCP server '{server_name}' not configured")

        server = self.servers[server_name]

        try:
            payload = {
                "tool": tool_name,
                "arguments": arguments
            }

            # Add authentication headers if needed
            headers = {}
            if server_name == 'context7' and 'CONTEXT7_API_KEY' in os.environ:
                headers['CONTEXT7_API_KEY'] = os.environ['CONTEXT7_API_KEY']

            response = await self.client.post(
                f"{server.url}/tools/call",
                json=payload,
                headers=headers
            )

            if response.status_code == 200:
                return response.json()
            else:
                raise Exception(f"MCP tool call failed: {response.status_code} - {response.text}")

        except Exception as e:
            raise Exception(f"Failed to call {tool_name} on {server_name}: {e}")

    async def context7_get_docs(self, library: str, version: Optional[str] = None) -> Dict:
        """Get library documentation using context7"""
        arguments = {"library": library}
        if version:
            arguments["version"] = version

        return await self.call_tool("context7", "get_documentation", arguments)

    async def serena_search(self, query: str, file_types: Optional[List[str]] = None) -> List[Dict]:
        """Search codebase using Serena"""
        arguments = {"query": query}
        if file_types:
            arguments["file_types"] = file_types

        return await self.call_tool("serena", "semantic_search", arguments)

    async def serena_analyze_file(self, file_path: str) -> Dict:
        """Analyze specific file using Serena"""
        arguments = {"file_path": file_path}

        return await self.call_tool("serena", "analyze_file", arguments)

    async def puppeteer_navigate(self, url: str, wait_for: Optional[str] = None) -> Dict:
        """Navigate to URL using puppeteer"""
        arguments = {"url": url}
        if wait_for:
            arguments["wait_for"] = wait_for

        return await self.call_tool("puppeteer", "navigate", arguments)

    async def puppeteer_screenshot(self, selector: Optional[str] = None) -> str:
        """Take screenshot using puppeteer"""
        arguments = {}
        if selector:
            arguments["selector"] = selector

        result = await self.call_tool("puppeteer", "screenshot", arguments)
        return result.get("screenshot_base64", "")

    async def close(self):
        """Clean up resources"""
        await self.client.aclose()

def create_mcp_integration(agent_config: Dict[str, Any]) -> Optional[MCPIntegration]:
    """Factory function to create MCP integration from agent config"""
    if 'mcp_servers' not in agent_config:
        return None

    return MCPIntegration(agent_config)