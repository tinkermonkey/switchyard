"""
Pipeline Factory for creating agents with MCP integration using new configuration system
"""

import os
from typing import Dict, Any, List, Optional
from pipeline.orchestrator import SequentialPipeline
from agents import AGENT_REGISTRY, get_agent_class
from agents.orchestrator_integration import AgentStage
from state_management.manager import StateManager
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
            'mcp_servers': []
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

        return AgentStage(agent_name, agent_config_dict)

    def create_pipeline_from_template(self, template_name: str, project_name: str) -> SequentialPipeline:
        """Create a pipeline from a template for a specific project"""

        template = self.config_manager.get_pipeline_template(template_name)
        stages = []

        for stage in template.stages:
            # Create maker agent
            maker_agent = self.create_agent(stage.default_agent, project_name)
            stages.append(maker_agent)

            # Create reviewer agent if required
            if stage.review_required and stage.reviewer_agent:
                reviewer_agent = self.create_agent(stage.reviewer_agent, project_name)
                stages.append(reviewer_agent)

        state_manager = StateManager()
        return SequentialPipeline(stages, state_manager)

    def create_project_pipeline(self, project_name: str, pipeline_name: str) -> SequentialPipeline:
        """Create a specific pipeline for a project"""

        project_config = self.config_manager.get_project_config(project_name)

        # Find the pipeline configuration
        pipeline_config = None
        for pipeline in project_config.pipelines:
            if pipeline.name == pipeline_name:
                pipeline_config = pipeline
                break

        if not pipeline_config:
            raise ValueError(f"Pipeline '{pipeline_name}' not found in project '{project_name}'")

        return self.create_pipeline_from_template(pipeline_config.template, project_name)


def create_pipeline_from_config(config_manager: ConfigManager) -> PipelineFactory:
    """Create a pipeline factory from configuration manager"""
    return PipelineFactory(config_manager)