"""
Review Status Parser

Parses agent review feedback to extract status, findings, and recommendations.
"""

import re
import logging
from typing import Dict, Any, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class ReviewStatus(Enum):
    """Review status values"""
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"
    PENDING = "pending"
    UNKNOWN = "unknown"


class ReviewFinding:
    """Represents a single review finding"""

    def __init__(self, category: str, severity: str, message: str, suggestion: Optional[str] = None):
        self.category = category
        self.severity = severity
        self.message = message
        self.suggestion = suggestion

    def to_dict(self) -> Dict[str, Any]:
        return {
            'category': self.category,
            'severity': self.severity,
            'message': self.message,
            'suggestion': self.suggestion
        }


class ReviewResult:
    """Parsed review result"""

    def __init__(
        self,
        status: ReviewStatus,
        findings: List[ReviewFinding],
        score: float = 0.0,
        summary: str = "",
        blocking_count: int = 0,
        high_severity_count: int = 0
    ):
        self.status = status
        self.findings = findings
        self.score = score
        self.summary = summary
        self.blocking_count = blocking_count
        self.high_severity_count = high_severity_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status.value,
            'findings': [f.to_dict() for f in self.findings],
            'score': self.score,
            'summary': self.summary,
            'blocking_count': self.blocking_count,
            'high_severity_count': self.high_severity_count
        }


class ReviewParser:
    """Parse agent review output to extract structured feedback"""

    # Status detection patterns
    # CRITICAL: ONLY match explicit "Status: VALUE" declarations.
    # Do NOT use fuzzy keyword matching - it causes false positives.
    # If no explicit status is found, we infer from findings count in parse_review().
    STATUS_PATTERNS = {
        ReviewStatus.APPROVED: [
            # ONLY explicit status declarations anchored to "status" keyword
            r'(?i)status[\s:]+\*{0,2}approved\*{0,2}',  # Matches "Status: APPROVED", "### Status\n**APPROVED**", etc.
        ],
        ReviewStatus.BLOCKED: [
            # ONLY explicit status declarations anchored to "status" keyword
            r'(?i)status[\s:]+\*{0,2}blocked\*{0,2}',  # Matches "Status: BLOCKED", "### Status\n**BLOCKED**", etc.
        ],
        ReviewStatus.CHANGES_REQUESTED: [
            # ONLY explicit status declarations anchored to "status" keyword
            r'(?i)status[\s:]+\*{0,2}changes?\s+(?:requested|needed)\*{0,2}',  # Matches "Status: CHANGES NEEDED", "### Status\n**CHANGES REQUESTED**", etc.
        ]
    }

    # Content-based inference patterns for when no explicit status is found
    CONTENT_INFERENCE_PATTERNS = {
        ReviewStatus.APPROVED: [
            r'(?i)\bno\s+(?:blocking\s+)?issues?\s+(?:found|identified)\b',
            r'(?i)\ball\s+checks?\s+(?:have\s+)?passed\b',
            r'(?i)\bready\s+to\s+proceed\b',
            r'(?i)\b(?:looks?|appears?)\s+good\b'
        ],
        ReviewStatus.BLOCKED: [
            r'(?i)\bcannot\s+proceed\b',
            r'(?i)\bmust\s+(?:be\s+)?(?:addressed|fixed|resolved)\b',
            r'(?i)(?<!non[-\s])\bblocking\s+issues?\s+(?:remain|exist|identified)\b',  # Negative lookbehind for "non-"
            r'(?i)(?<!non[-\s])\bcritical\s+issues?\s+(?:remain|exist|identified)\b'  # Negative lookbehind for "non-"
        ],
        ReviewStatus.CHANGES_REQUESTED: [
            r'(?i)\bnon[-\s]?blocking\s+issues?\b',
            r'(?i)\bplease\s+(?:address|fix|consider)\b',
            r'(?i)\bsuggestions?\s+for\s+improvement\b'
        ]
    }

    # Severity indicators
    SEVERITY_MARKERS = {
        'blocking': r'(?i)(?<!non[-\s])\b(?:blocking|critical|must\s+fix|blocker)\b',  # Negative lookbehind for "non-"
        'high': r'(?i)\b(?:high|major|important|should\s+fix)\b',
        'medium': r'(?i)\b(?:medium|moderate|consider)\b',
        'low': r'(?i)\b(?:low|minor|nice\s+to\s+have|nitpick)\b'
    }

    def __init__(self):
        pass

    def parse_review(self, review_comment_body: str) -> ReviewResult:
        """
        Parse a review comment and extract structured feedback

        Args:
            review_comment_body: The full review comment text

        Returns:
            ReviewResult with parsed status and findings
        """
        # Extract status
        status = self._extract_status(review_comment_body)

        # Extract findings
        findings = self._extract_findings(review_comment_body)

        # Count severity levels
        blocking_count = sum(1 for f in findings if f.severity == 'blocking')
        high_severity_count = sum(1 for f in findings if f.severity == 'high')

        # Extract quality score if present
        score = self._extract_score(review_comment_body)

        # Extract summary
        summary = self._extract_summary(review_comment_body)

        # If status is unknown, infer from findings
        if status == ReviewStatus.UNKNOWN:
            if blocking_count > 0:
                status = ReviewStatus.BLOCKED
            elif high_severity_count > 0 or len(findings) > 0:
                status = ReviewStatus.CHANGES_REQUESTED
            elif len(findings) == 0:
                status = ReviewStatus.APPROVED
            else:
                status = ReviewStatus.PENDING

        return ReviewResult(
            status=status,
            findings=findings,
            score=score,
            summary=summary,
            blocking_count=blocking_count,
            high_severity_count=high_severity_count
        )

    def _extract_status(self, text: str) -> ReviewStatus:
        """
        Extract review status from text

        First tries explicit "Status: VALUE" declarations.
        If none found, uses content-based inference patterns.
        If still none found, returns UNKNOWN for findings-based inference.
        """
        # Remove inline markdown formatting (bold, italic) to match patterns
        # e.g., "**Status**: **BLOCKED**" becomes "Status: BLOCKED"
        text_clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Remove **bold**
        text_clean = re.sub(r'\*([^*]+)\*', r'\1', text_clean)      # Remove *italic*
        text_clean = re.sub(r'_([^_]+)_', r'\1', text_clean)        # Remove _italic_

        # Check for explicit status declarations FIRST
        for status in [ReviewStatus.APPROVED, ReviewStatus.BLOCKED, ReviewStatus.CHANGES_REQUESTED]:
            pattern = self.STATUS_PATTERNS[status][0]  # Only one pattern per status now
            if re.search(pattern, text_clean):
                logger.info(f"Matched explicit status declaration: {status.value} (pattern: {pattern})")
                return status

        # If no explicit status found, try content-based inference
        for status in [ReviewStatus.APPROVED, ReviewStatus.BLOCKED, ReviewStatus.CHANGES_REQUESTED]:
            for pattern in self.CONTENT_INFERENCE_PATTERNS[status]:
                if re.search(pattern, text_clean):
                    logger.info(f"Inferred status from content: {status.value} (pattern: {pattern})")
                    return status

        logger.info("No explicit status declaration or content inference found, will infer from findings")
        return ReviewStatus.UNKNOWN

    def _extract_findings(self, text: str) -> List[ReviewFinding]:
        """Extract review findings from text"""
        findings = []

        # Look for structured findings sections
        # Pattern 1: Severity-based headers (our agent's format)
        # e.g., "### Critical (Must Fix)", "#### High Priority (Should Fix)"
        has_severity_sections = bool(re.search(
            r'(?m)^\s*#{1,6}\s+(?:Critical|High\s+Priority|Medium\s+Priority|Low\s+Priority)',
            text
        ))

        # Pattern 2: Generic issue sections
        # e.g., "## Issues Found", "### Review Findings"
        has_generic_sections = bool(re.search(
            r'(?m)^\s*#{1,6}\s+(?:Review\s+)?(?:Issues?|Findings?|Problems?|Concerns?)',
            text
        ))

        # Pattern 3: Emoji-based or text-based structured sections
        # e.g., "🚫 One blocking issue:", "One blocking issue:", "High priority:"
        has_emoji_structure = bool(re.search(
            r'(?m)^\s*(?:(?:🚫|⚠️|⚡)\s+)?(?:One|Multiple|Some)?\s*(?:blocking|high\s+priority|medium\s+priority|low\s+priority).*?:\s*$.*?^\s*[•\-\*]\s+',
            text,
            re.IGNORECASE | re.DOTALL
        ))

        if has_severity_sections or has_generic_sections or has_emoji_structure:
            # Parse as structured findings (handles severity sections natively)
            findings.extend(self._parse_findings_list(text))
        else:
            # No structured section found, look for inline findings
            findings.extend(self._parse_inline_findings(text))

        # Deduplicate findings by message (in case of any overlap)
        unique_findings = []
        seen_messages = set()
        for finding in findings:
            if finding.message not in seen_messages:
                unique_findings.append(finding)
                seen_messages.add(finding.message)

        return unique_findings

    def _parse_findings_list(self, text: str) -> List[ReviewFinding]:
        """Parse a list of findings from structured text"""
        findings = []

        # Match bullet points
        lines = text.split('\n')
        current_finding = None
        current_section_severity = None  # Track severity from section headers
        current_section_is_ceiling = False  # True for advisory sections: severity is a cap, not a floor

        for line in lines:
            line = line.strip()

            # Check if this is a section header:
            # Case 1: Emoji-based or text-based header (e.g., "🚫 One blocking issue:", "One blocking issue:", "High priority:")
            # Must end with colon and contain severity keywords
            emoji_or_text_header_match = re.match(
                r'^(?:#{1,6}\s*)?(?:(?:🚫|⚠️|⚡|✅)\s+)?((?:One|Multiple|Some)?\s*(?:blocking|high\s+priority|medium\s+priority|low\s+priority).*?):\s*$',
                line,
                re.IGNORECASE
            )
            if emoji_or_text_header_match:
                section_title = emoji_or_text_header_match.group(1).strip()
                current_section_severity = self._extract_severity(section_title)
                current_section_is_ceiling = False
                continue

            # Case 2: Markdown header with severity keywords (e.g., "### Blocking Issues")
            markdown_header_match = re.match(r'^#{1,6}\s+(.+)', line)
            if markdown_header_match:
                section_title = markdown_header_match.group(1).strip()
                # Advisory/FYI sections cap findings at low severity — items cannot escalate
                if re.search(r'(?i)\b(?:advisory|out\s+of\s+scope|fyi)\b', section_title):
                    current_section_severity = 'low'
                    current_section_is_ceiling = True
                    continue
                current_section_is_ceiling = False
                severity = self._extract_severity(section_title)
                if severity != 'medium':  # Has a severity keyword (blocking/high/low)
                    current_section_severity = severity
                    continue

            # Check if this is a new finding (starts with bullet point)
            if re.match(r'^[•\-\*]\s+', line):
                # Save previous finding if exists
                if current_finding:
                    findings.append(current_finding)

                # Parse this finding
                finding_text = re.sub(r'^[•\-\*]\s+', '', line)

                # Extract severity from finding text
                finding_severity = self._extract_severity(finding_text)

                # Determine effective severity:
                # - Advisory sections act as a ceiling: cap at section severity (low)
                # - Other sections act as a floor: use whichever is higher
                severity_priority = {'blocking': 4, 'high': 3, 'medium': 2, 'low': 1}
                section_priority = severity_priority.get(current_section_severity, 0)
                finding_priority = severity_priority.get(finding_severity, 0)

                if current_section_is_ceiling:
                    # Cap at section severity (advisory items cannot escalate beyond low)
                    severity = current_section_severity if section_priority <= finding_priority else finding_severity
                elif section_priority > finding_priority:
                    severity = current_section_severity
                else:
                    severity = finding_severity

                # Extract category (usually in bold or at start)
                category_match = re.match(r'\*\*([^:*]+)\*\*:?\s*', finding_text)
                if category_match:
                    category = category_match.group(1).strip()
                    message = finding_text[category_match.end():].strip()
                else:
                    category = 'general'
                    message = finding_text

                current_finding = ReviewFinding(
                    category=category,
                    severity=severity,
                    message=message,
                    suggestion=None
                )

            # Check if this is a suggestion for the current finding (line already stripped)
            elif current_finding and re.match(r'^(?:💡|Suggestion:|Recommended:)', line, re.IGNORECASE):
                suggestion_text = re.sub(r'^(?:💡|Suggestion:|Recommended:)\s*', '', line, flags=re.IGNORECASE)
                current_finding.suggestion = suggestion_text.strip()

        # Don't forget the last finding
        if current_finding:
            findings.append(current_finding)

        return findings

    def _parse_inline_findings(self, text: str) -> List[ReviewFinding]:
        """Parse findings from inline text (less structured)"""
        findings = []

        # Patterns that indicate issues are RESOLVED, not current findings
        resolved_patterns = [
            r'(?i)\b(?:addressed|addressing|resolved|resolving|fixed|fixing)\b.*\b(?:blocking|critical|issues?)\b',
            r'(?i)\b(?:blocking|critical|issues?)\b.*\b(?:addressed|resolved|fixed|have been|has been)\b',
            r'(?i)\ball\s+(?:blocking|critical)?\s*issues?\s+(?:addressed|resolved|fixed)',
        ]

        # Negative indicators that suggest an actual problem/finding
        negative_indicators = [
            r'(?i)\b(?:missing|lacking|unclear|insufficient|ambiguous|incomplete|undefined|unspecified)\b',
            r'(?i)\b(?:needs?|requires?|must|should)\s+(?:to\s+)?(?:fix|address|add|define|specify|clarify)\b',
            r'(?i)\b(?:no|not|without|fails?\s+to)\b',
            r'(?i)\b(?:gap|problem|issue|concern|risk)\b',
        ]

        # Look for lines with severity markers
        lines = text.split('\n')
        for line in lines:
            # Skip empty lines and headings
            if not line.strip() or re.match(r'^#{1,6}\s', line):
                continue

            # Skip lines that talk about resolved issues
            if any(re.search(pattern, line) for pattern in resolved_patterns):
                continue

            # Check if line contains a severity marker
            severity = self._extract_severity(line)

            # Only include if we found a severity marker AND negative indicator
            # (prevents false positives like "All critical requirements now specified")
            if severity in ['blocking', 'high']:
                has_negative = any(re.search(pattern, line) for pattern in negative_indicators)
                if not has_negative:
                    continue  # Skip positive statements with severity keywords
                # Try to extract category and message
                # Look for patterns like "**Category**: message" or "emoji Category: message"
                category_patterns = [
                    r'[🚫❗⚠️]\s*\*\*([^:*]+)\*\*:?\s*(.+)',
                    r'\*\*([^:*]+)\*\*:?\s*(.+)',
                    r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*):?\s+(.+)'
                ]

                matched = False
                for pattern in category_patterns:
                    match = re.search(pattern, line)
                    if match:
                        category = match.group(1).strip()
                        message = match.group(2).strip()
                        findings.append(ReviewFinding(
                            category=category,
                            severity=severity,
                            message=message
                        ))
                        matched = True
                        break

                if not matched:
                    # Just use the whole line as message
                    findings.append(ReviewFinding(
                        category='general',
                        severity=severity,
                        message=line.strip()
                    ))

        return findings

    def _extract_severity(self, text: str) -> str:
        """Extract severity level from text"""
        for severity, pattern in self.SEVERITY_MARKERS.items():
            if re.search(pattern, text):
                return severity
        return 'medium'  # Default to medium if no severity marker found

    def _extract_score(self, text: str) -> float:
        """Extract quality score if present"""
        # Look for patterns like "Score: 85%", "Quality: 0.85", "8.5/10"
        # Check /10 pattern first as it's more specific
        patterns = [
            (r'(?i)(?:score|quality|rating):\s*(\d+(?:\.\d+)?)\s*/\s*10', 10),  # "Rating: 8.5/10" -> divide by 10
            (r'(?i)(?:score|quality|rating):\s*(\d+(?:\.\d+)?)%', 100),  # "Score: 85%" -> divide by 100
            (r'(?i)(?:score|quality|rating):\s*(\d+(?:\.\d+)?)', 100),   # "Score: 0.85" -> divide by 100 if > 1
            (r'(\d+(?:\.\d+)?)\s*/\s*10', 10),                           # "8.5/10" -> divide by 10
        ]

        for pattern, divisor in patterns:
            match = re.search(pattern, text)
            if match:
                value = float(match.group(1))
                # Normalize to 0-1 range
                if value > 1:
                    value = value / divisor
                return value

        return 0.0

    def _extract_summary(self, text: str) -> str:
        """Extract review summary"""
        # Look for summary section (allow leading whitespace for indented markdown)
        summary_match = re.search(
            r'(?m)^\s*#{1,3}\s+(?:Summary|Overall|Conclusion)\s*\n\s*(.+?)(?=\n\s*#{1,3}\s|\Z)',
            text,
            re.DOTALL
        )

        if summary_match:
            return summary_match.group(1).strip()

        # Fallback: use first paragraph
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
        if paragraphs:
            return paragraphs[0]

        return ""


# Global parser instance
review_parser = ReviewParser()
