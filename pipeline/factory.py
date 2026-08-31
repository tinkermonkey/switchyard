"""
Pipeline Factory for creating agents with MCP integration using new configuration system
"""

import os
from typing import Dict, Any, List, Optional
from agents import AGENT_REGISTRY, get_agent_class
from agents.orchestrator_integration import AgentStage
from config.manager import ConfigManager, PipelineTemplate


class PipelineFactory:
    """Factory for creating pipelines from configuration templates"""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager

    def create_agent(self, agent_name: str, project_name: str) -> AgentStage:
        """Create an agent instance with project-specific configuration"""

        agent_config = self.config_manager.get_project_agent_config(project_name, agent_name)

        # Convert to format expected by AgentStage
        agent_config_dict = {
            'claude_model': agent_config.model,
            'timeout': agent_config.timeout,
            'working_directory': agent_config.working_directory,
            'output_format': agent_config.output_format,
            'tools_enabled': agent_config.tools_enabled,
            'mcp_servers': [],
            'agent_config': agent_config  # Pass full config for security checks
        }

        # Process MCP server configurations
        for server in agent_config.mcp_servers:
            server_config = server.copy()
            # Expand environment variables in URL for HTTP servers
            if 'url' in server_config:
                server_config['url'] = os.path.expandvars(server_config['url'])
            agent_config_dict['mcp_servers'].append(server_config)

        # Use the agent registry to validate agent exists
        agent_class = get_agent_class(agent_name)
        if not agent_class:
            raise ValueError(f"Unknown agent type: {agent_name}")

        return AgentStage(agent_name, agent_config_dict, project_name=project_name)


def create_pipeline_from_config(config_manager: ConfigManager) -> PipelineFactory:
    """Create a pipeline factory from configuration manager"""
    return PipelineFactory(config_manager)