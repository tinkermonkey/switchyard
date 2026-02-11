from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class TechnicalWriterAgent(MakerAgent):
    """
    Technical Writer agent for documentation creation.

    Creates clear, accurate technical documentation following best practices.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("technical_writer", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Technical Writer"

    @property
    def agent_role_description(self) -> str:
        return "I create clear, accurate technical documentation including API docs, user guides, tutorials, and knowledge base content following documentation best practices for clarity and completeness."

    @property
    def output_sections(self) -> List[str]:
        return [
            "API Documentation",
            "User Documentation",
            "Developer Documentation",
            "System Documentation",
            "Operations Documentation"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- Documentation is clear, accurate, and complete
- API documentation follows OpenAPI/Swagger standards
- User guides include step-by-step tutorials
- Code examples are functional and well-explained
- Documentation is maintained and version-controlled
"""

    def get_initial_guidelines(self) -> str:
        """Technical writing guidelines and best practices"""
        return """
**Documentation Creation Guidelines**:

**Scope & Focus**:
- Write ONLY the documentation requested in the requirements
- Don't create additional "helpful" sections that weren't asked for
- Re-use existing documentation structure and patterns
- Link to existing docs rather than duplicating content

**Clarity & Precision**:
- Start with concrete examples, then explain concepts
- Use active voice ("Click Submit" not "The Submit button should be clicked")
- Define technical terms on first use
- Keep sentences under 25 words where possible

**Code Examples**:
- Every API endpoint needs a working curl example
- Every code snippet must be runnable (include imports, setup)
- Show both success and error cases
- Include expected output

**Structure**:
- Use descriptive section names (not "Overview", "Details", "Additional Info")
- One concept per section
- Most important information first (inverted pyramid)

**Anti-Patterns to Avoid**:
- ❌ "Introduction" or "Overview" sections that don't add value
- ❌ Explaining what the reader already knows ("Git is a version control system...")
- ❌ Speculative sections ("Future Enhancements", "Roadmap")
- ❌ Marketing language ("revolutionary", "seamless", "effortless")
- ❌ Placeholder content ("TBD", "Coming soon", "To be documented")
- ❌ Documenting implementation details users don't need
- ❌ Creating separate "Examples" section when examples should be inline
"""
