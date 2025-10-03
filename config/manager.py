"""
Configuration Manager for the Claude Code Orchestrator

This module provides the core configuration management functionality, handling:
- Loading foundational configurations (agents, pipelines, workflows)
- Loading project-specific configurations
- Merging configurations with proper inheritance
- Validating configurations for consistency
"""

import os
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Agent configuration data"""
    name: str
    description: str
    model: str
    timeout: int
    retries: int
    capabilities: List[str]
    tools_enabled: List[str]
    mcp_servers: List[Dict[str, Any]]
    working_directory: str
    output_format: str
    makes_code_changes: bool = False
    requires_dev_container: bool = False
    requires_docker: bool = True  # CRITICAL: Default to True for security, only dev_environment_setup should be False
    filesystem_write_allowed: bool = True  # Default to True for backward compatibility


@dataclass
class PipelineStage:
    """Pipeline stage configuration"""
    stage: str
    name: str
    required_capabilities: List[str]
    default_agent: str
    timeout: int
    retries: int
    quality_gates: Dict[str, float]
    review_required: bool
    reviewer_agent: Optional[str] = None
    reviewer_timeout: Optional[int] = None
    reviewer_retries: Optional[int] = None
    escalation: Optional[Dict[str, Any]] = None


@dataclass
class PipelineTemplate:
    """Pipeline template configuration"""
    name: str
    description: str
    workflow_type: str
    stages: List[PipelineStage]
    workspace: str = "issues"  # "issues", "discussions", or "hybrid"
    discussion_category: Optional[str] = None
    discussion_stages: Optional[List[str]] = None
    issue_stages: Optional[List[str]] = None


@dataclass
class WorkflowColumn:
    """Workflow column configuration"""
    name: str
    stage_mapping: Optional[str]
    agent: Optional[str]
    description: str
    automation_rules: List[Dict[str, Any]]
    type: Optional[str] = None  # "maker", "review", or None
    maker_agent: Optional[str] = None  # For review columns: which agent to send feedback to
    max_iterations: int = 3  # For review columns: max revision cycles
    auto_advance_on_approval: bool = True  # For review columns: auto-move on approval
    escalate_on_blocked: bool = True  # For review columns: escalate blocking issues


@dataclass
class WorkflowTemplate:
    """Workflow template configuration"""
    name: str
    description: str
    pipeline_mapping: str
    columns: List[WorkflowColumn]


@dataclass
class ProjectPipeline:
    """Project pipeline instance configuration"""
    template: str
    name: str
    board_name: str
    description: str
    workflow: str
    active: bool
    workspace: str = "issues"  # "issues", "discussions", or "hybrid"
    discussion_category: Optional[str] = None  # For discussions workspace
    discussion_stages: Optional[List[str]] = None  # For hybrid workspace
    issue_stages: Optional[List[str]] = None  # For hybrid workspace
    auto_create_from_issues: bool = True  # Auto-create discussion when issue added
    update_issue_on_completion: bool = True  # Update issue with final requirements
    discussion_title_prefix: str = "Requirements: "  # Prefix for discussion titles
    transition_stage: Optional[str] = None  # Stage at which to transition (for hybrid)


@dataclass
class ProjectConfig:
    """Project configuration data"""
    name: str
    description: str
    github: Dict[str, str]
    tech_stacks: Dict[str, str]
    pipelines: List[ProjectPipeline]
    pipeline_routing: Dict[str, Any]
    agent_customizations: Dict[str, Dict[str, Any]]
    orchestrator: Dict[str, Any]


class ConfigurationError(Exception):
    """Configuration-related error"""
    pass


class ConfigManager:
    """
    Main configuration manager that loads and merges all configuration files
    """

    def __init__(self, config_root: Optional[str] = None):
        """Initialize configuration manager

        Args:
            config_root: Root directory for configuration files. Defaults to ./config
        """
        if config_root is None:
            config_root = Path(__file__).parent

        self.config_root = Path(config_root)
        self.foundations_dir = self.config_root / "foundations"
        self.projects_dir = self.config_root / "projects"

        # Cached configurations
        self._agents: Optional[Dict[str, AgentConfig]] = None
        self._mcp_servers: Optional[Dict[str, Dict[str, Any]]] = None
        self._pipeline_templates: Optional[Dict[str, PipelineTemplate]] = None
        self._workflow_templates: Optional[Dict[str, WorkflowTemplate]] = None
        self._project_configs: Dict[str, ProjectConfig] = {}

    def _load_yaml(self, file_path: Path) -> Dict[str, Any]:
        """Load and parse YAML file"""
        try:
            with open(file_path, 'r') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            raise ConfigurationError(f"Configuration file not found: {file_path}")
        except yaml.YAMLError as e:
            raise ConfigurationError(f"Invalid YAML in {file_path}: {e}")

    def _load_mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """Load MCP server definitions from foundations"""
        mcp_file = self.foundations_dir / "mcp.yaml"
        data = self._load_yaml(mcp_file)
        return data.get('mcp_servers', {})

    def _resolve_mcp_servers(self, server_names: List[str]) -> List[Dict[str, Any]]:
        """Resolve MCP server names to their full configurations"""
        if self._mcp_servers is None:
            self._mcp_servers = self._load_mcp_servers()

        resolved_servers = []
        for name in server_names:
            if isinstance(name, str):
                # Server specified by name only - resolve it
                if name in self._mcp_servers:
                    server_config = self._mcp_servers[name].copy()
                    server_config['name'] = name
                    resolved_servers.append(server_config)
                else:
                    logger.warning(f"MCP server '{name}' not found in mcp.yaml")
            elif isinstance(name, dict):
                # Server specified with full config (legacy format) - use as-is
                resolved_servers.append(name)

        return resolved_servers

    def _load_agents(self) -> Dict[str, AgentConfig]:
        """Load agent configurations from foundations"""
        agents_file = self.foundations_dir / "agents.yaml"
        data = self._load_yaml(agents_file)

        agents = {}
        default_config = data.get('default_config', {})

        for name, config in data['agents'].items():
            # Merge with defaults
            merged_config = {**default_config, **config}

            # Resolve MCP server names to full configurations
            mcp_server_refs = config.get('mcp_servers', [])
            resolved_mcp_servers = self._resolve_mcp_servers(mcp_server_refs)

            agents[name] = AgentConfig(
                name=name,
                description=config['description'],
                model=config['model'],
                timeout=config['timeout'],
                retries=config['retries'],
                capabilities=config['capabilities'],
                tools_enabled=config['tools_enabled'],
                mcp_servers=resolved_mcp_servers,
                working_directory=merged_config.get('working_directory', '/workspace/{project_name}'),
                output_format=merged_config.get('output_format', 'structured_json'),
                makes_code_changes=config.get('makes_code_changes', False),
                requires_dev_container=config.get('requires_dev_container', False)
            )

        return agents

    def _load_pipeline_templates(self) -> Dict[str, PipelineTemplate]:
        """Load pipeline templates from foundations"""
        pipelines_file = self.foundations_dir / "pipelines.yaml"
        data = self._load_yaml(pipelines_file)

        templates = {}

        for name, template_data in data['pipeline_templates'].items():
            stages = []
            for stage_data in template_data['stages']:
                stages.append(PipelineStage(
                    stage=stage_data['stage'],
                    name=stage_data['name'],
                    required_capabilities=stage_data['required_capabilities'],
                    default_agent=stage_data['default_agent'],
                    timeout=stage_data['timeout'],
                    retries=stage_data['retries'],
                    quality_gates=stage_data['quality_gates'],
                    review_required=stage_data.get('review_required', False),
                    reviewer_agent=stage_data.get('reviewer_agent'),
                    reviewer_timeout=stage_data.get('reviewer_timeout'),
                    reviewer_retries=stage_data.get('reviewer_retries'),
                    escalation=stage_data.get('escalation')
                ))

            templates[name] = PipelineTemplate(
                name=template_data['name'],
                description=template_data['description'],
                workflow_type=template_data['workflow_type'],
                stages=stages,
                workspace=template_data.get('workspace', 'issues'),
                discussion_category=template_data.get('discussion_category'),
                discussion_stages=template_data.get('discussion_stages'),
                issue_stages=template_data.get('issue_stages')
            )

        return templates

    def _load_workflow_templates(self) -> Dict[str, WorkflowTemplate]:
        """Load workflow templates from foundations"""
        workflows_file = self.foundations_dir / "workflows.yaml"
        data = self._load_yaml(workflows_file)

        templates = {}

        for name, template_data in data['workflow_templates'].items():
            columns = []
            for column_data in template_data['columns']:
                columns.append(WorkflowColumn(
                    name=column_data['name'],
                    stage_mapping=column_data.get('stage_mapping'),
                    agent=column_data.get('agent'),
                    description=column_data['description'],
                    automation_rules=column_data.get('automation_rules', []),
                    type=column_data.get('type'),
                    maker_agent=column_data.get('maker_agent'),
                    max_iterations=column_data.get('max_iterations', 3),
                    auto_advance_on_approval=column_data.get('auto_advance_on_approval', True),
                    escalate_on_blocked=column_data.get('escalate_on_blocked', True)
                ))

            templates[name] = WorkflowTemplate(
                name=template_data['name'],
                description=template_data['description'],
                pipeline_mapping=template_data['pipeline_mapping'],
                columns=columns
            )

        return templates

    def _load_project_config(self, project_name: str) -> ProjectConfig:
        """Load project-specific configuration"""
        project_file = self.projects_dir / f"{project_name}.yaml"
        data = self._load_yaml(project_file)

        project_data = data['project']

        # Parse pipeline configurations
        pipelines = []
        for pipeline_data in project_data['pipelines']['enabled']:
            # Get template to inherit workspace settings if not overridden
            template_name = pipeline_data['template']
            template = self.get_pipeline_templates().get(template_name)

            # Inherit from template if not specified in project config
            workspace = pipeline_data.get('workspace')
            if workspace is None and template:
                workspace = template.workspace
            if workspace is None:
                workspace = 'issues'

            discussion_category = pipeline_data.get('discussion_category')
            if discussion_category is None and template:
                discussion_category = template.discussion_category

            discussion_stages = pipeline_data.get('discussion_stages')
            if discussion_stages is None and template:
                discussion_stages = template.discussion_stages

            issue_stages = pipeline_data.get('issue_stages')
            if issue_stages is None and template:
                issue_stages = template.issue_stages

            pipelines.append(ProjectPipeline(
                template=template_name,
                name=pipeline_data['name'],
                board_name=pipeline_data['board_name'],
                description=pipeline_data['description'],
                workflow=pipeline_data['workflow'],
                active=pipeline_data['active'],
                workspace=workspace,
                discussion_category=discussion_category,
                discussion_stages=discussion_stages,
                issue_stages=issue_stages
            ))

        return ProjectConfig(
            name=project_data['name'],
            description=project_data['description'],
            github=project_data['github'],
            tech_stacks=project_data['tech_stacks'],
            pipelines=pipelines,
            pipeline_routing=project_data['pipeline_routing'],
            agent_customizations=project_data.get('agent_customizations', {}),
            orchestrator=data.get('orchestrator', {})
        )

    def get_agents(self) -> Dict[str, AgentConfig]:
        """Get all agent configurations"""
        if self._agents is None:
            self._agents = self._load_agents()
        return self._agents

    def get_agent(self, agent_name: str) -> AgentConfig:
        """Get specific agent configuration"""
        agents = self.get_agents()
        if agent_name not in agents:
            raise ConfigurationError(f"Agent not found: {agent_name}")
        return agents[agent_name]

    def get_pipeline_templates(self) -> Dict[str, PipelineTemplate]:
        """Get all pipeline templates"""
        if self._pipeline_templates is None:
            self._pipeline_templates = self._load_pipeline_templates()
        return self._pipeline_templates

    def get_pipeline_template(self, template_name: str) -> PipelineTemplate:
        """Get specific pipeline template"""
        templates = self.get_pipeline_templates()
        if template_name not in templates:
            raise ConfigurationError(f"Pipeline template not found: {template_name}")
        return templates[template_name]

    def get_workflow_templates(self) -> Dict[str, WorkflowTemplate]:
        """Get all workflow templates"""
        if self._workflow_templates is None:
            self._workflow_templates = self._load_workflow_templates()
        return self._workflow_templates

    def get_workflow_template(self, template_name: str) -> WorkflowTemplate:
        """Get specific workflow template"""
        templates = self.get_workflow_templates()
        if template_name not in templates:
            raise ConfigurationError(f"Workflow template not found: {template_name}")
        return templates[template_name]

    def get_project_config(self, project_name: str) -> ProjectConfig:
        """Get project configuration"""
        if project_name not in self._project_configs:
            self._project_configs[project_name] = self._load_project_config(project_name)
        return self._project_configs[project_name]

    def get_project_agent_config(self, project_name: str, agent_name: str) -> AgentConfig:
        """Get agent configuration with project-specific customizations applied"""
        base_agent = self.get_agent(agent_name)
        project_config = self.get_project_config(project_name)

        # Apply project customizations
        customizations = project_config.agent_customizations.get(agent_name, {})

        # Create a new agent config with customizations
        return AgentConfig(
            name=base_agent.name,
            description=base_agent.description,
            model=base_agent.model,
            timeout=customizations.get('timeout', base_agent.timeout),
            retries=customizations.get('retries', base_agent.retries),
            capabilities=base_agent.capabilities,
            tools_enabled=base_agent.tools_enabled,
            mcp_servers=base_agent.mcp_servers,
            working_directory=base_agent.working_directory.replace('{project_name}', project_name),
            output_format=base_agent.output_format,
            makes_code_changes=base_agent.makes_code_changes,
            requires_dev_container=base_agent.requires_dev_container
        )

    def get_project_pipelines(self, project_name: str) -> List[ProjectPipeline]:
        """Get enabled pipelines for a project"""
        project_config = self.get_project_config(project_name)
        return [p for p in project_config.pipelines if p.active]

    def get_project_workflow(self, project_name: str, board_name: str) -> WorkflowTemplate:
        """Get workflow template for a specific project board"""
        project_config = self.get_project_config(project_name)

        # Find the pipeline with matching board_name
        pipeline = next(
            (p for p in project_config.pipelines if p.board_name == board_name),
            None
        )

        if not pipeline:
            raise ConfigurationError(f"No pipeline found for board '{board_name}' in project '{project_name}'")

        # Get the workflow template for this pipeline
        return self.get_workflow_template(pipeline.workflow)

    def validate_project_config(self, project_name: str) -> List[str]:
        """Validate project configuration and return list of errors"""
        errors = []

        try:
            project_config = self.get_project_config(project_name)
        except ConfigurationError as e:
            return [f"Failed to load project config: {e}"]

        # Validate that referenced templates exist
        pipeline_templates = self.get_pipeline_templates()
        workflow_templates = self.get_workflow_templates()
        agents = self.get_agents()

        for pipeline in project_config.pipelines:
            if pipeline.template not in pipeline_templates:
                errors.append(f"Pipeline template not found: {pipeline.template}")

            if pipeline.workflow not in workflow_templates:
                errors.append(f"Workflow template not found: {pipeline.workflow}")

        # Validate agent customizations reference valid agents
        for agent_name in project_config.agent_customizations.keys():
            if agent_name not in agents:
                errors.append(f"Agent customization references unknown agent: {agent_name}")

        # Validate pipeline routing
        for pipeline_name in project_config.pipeline_routing['label_routing'].values():
            if not any(p.name == pipeline_name for p in project_config.pipelines):
                errors.append(f"Pipeline routing references unknown pipeline: {pipeline_name}")

        return errors

    def list_projects(self) -> List[str]:
        """List all available project configurations"""
        if not self.projects_dir.exists():
            return []

        projects = []
        for file_path in self.projects_dir.glob("*.yaml"):
            projects.append(file_path.stem)

        return sorted(projects)

    def reload_config(self):
        """Clear cached configurations and reload from disk"""
        self._agents = None
        self._pipeline_templates = None
        self._workflow_templates = None
        self._project_configs.clear()
        logger.info("Configuration cache cleared, will reload on next access")


# Global configuration manager instance
config_manager = ConfigManager()