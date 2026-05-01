from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LintConfig:
    anti_patterns: bool = True
    selector_registry_path: Optional[str] = None
    generated_types_paths: List[str] = field(default_factory=list)
    pre_flight_context_fetch: bool = True
    issue_body_conformance: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> 'LintConfig':
        return cls(
            anti_patterns=data.get('anti_patterns', True),
            selector_registry_path=data.get('selector_registry_path'),
            generated_types_paths=data.get('generated_types_paths', []),
            pre_flight_context_fetch=data.get('pre_flight_context_fetch', True),
            issue_body_conformance=data.get('issue_body_conformance', True),
        )
