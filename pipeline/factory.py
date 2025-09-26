"""
Pipeline Factory for creating agents with MCP integration
"""

import yaml
import os
from typing import Dict, Any, List
from pipeline.orchestrator import SequentialPipeline
from agents.business_analyst_agent import BusinessAnalystAgent
from state_management.manager import StateManager

def load_pipeline_config(config_path: str = "config/pipelines.yaml") -> Dict[str, Any]:
    """Load pipeline configuration from YAML file"""

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Expand environment variables in URLs
    for agent_name, agent_config in config.get('agent_configs', {}).items():
        if 'mcp_servers' in agent_config:
            for server in agent_config['mcp_servers']:
                server['url'] = os.path.expandvars(server['url'])

    return config

def create_agent(agent_name: str, config: Dict[str, Any]):
    """Create an agent instance with proper configuration"""

    agent_config = config.get('agent_configs', {}).get(agent_name, {})

    if agent_name == 'business_analyst':
        return BusinessAnalystAgent(agent_config)
    # Add other agent types as needed
    else:
        raise ValueError(f"Unknown agent type: {agent_name}")

def create_pipeline(pipeline_name: str, config: Dict[str, Any]) -> SequentialPipeline:
    """Create a pipeline with configured agents"""

    pipeline_config = config['pipelines'][pipeline_name]
    stages = []

    for stage_config in pipeline_config['agents']:
        agent_name = stage_config['name']
        agent = create_agent(agent_name, config)
        stages.append(agent)

    state_manager = StateManager()
    return SequentialPipeline(stages, state_manager)

def create_default_pipeline() -> SequentialPipeline:
    """Create the default pipeline from configuration"""

    config = load_pipeline_config()
    default_pipeline_name = config.get('default', 'business_analyst_only')

    return create_pipeline(default_pipeline_name, config)