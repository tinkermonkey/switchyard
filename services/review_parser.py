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
    # IMPORTANT: Patterns are checked in order. Explicit status declarations come first.
    STATUS_PATTERNS = {
        ReviewStatus.APPROVED: [
            # Explicit status declarations (highest priority)
            r'(?i)status:\s*approved',
            r'(?i)✅\s*approved',
            # Positive indicators
            r'(?i)all\s+(?:checks|criteria)\s+(?:have\s+)?passed',
            r'(?i)ready\s+to\s+proceed',
            r'(?i)no\s+(?:blocking\s+)?issues?\s+found',
            # Past issues now resolved (should be APPROVED not BLOCKED)
            r'(?i)(?:all|successfully)\s+(?:addressed|addressing|resolved|resolving)\s+(?:all\s+)?(?:critical\s+)?blocking\s+issues?',
        ],
        ReviewStatus.BLOCKED: [
            # Explicit status declarations (highest priority)
            r'(?i)status:\s*blocked',
            r'(?i)🚫\s*blocked',
            # Current blocking issues ONLY (not past issues)
            # MUST use present tense or future tense, NOT past tense
            r'(?i)(?:current|remaining|outstanding)\s+blocking\s+issues?',
            r'(?i)blocking\s+issues?\s+(?:remain|exist|present)',
            r'(?i)critical\s+(?:issues?|problems?)\s+(?:remain|exist|present)',
            r'(?i)cannot\s+proceed',
            r'(?i)must\s+(?:fix|address|resolve)\s+before\s+proceeding',
        ],
        ReviewStatus.CHANGES_REQUESTED: [
            # Explicit status declarations (highest priority)
            r'(?i)status:\s*changes?\s+requested',
            r'(?i)🔄\s*changes?\s+requested',
            # Non-blocking issues
            r'(?i)(?:non-blocking\s+)?issues?\s+(?:identified|found)',
            r'(?i)requires?\s+(?:minor\s+)?(?:changes?|revisions?)',
            r'(?i)please\s+address',
        ]
    }

    # Severity indicators
    SEVERITY_MARKERS = {
        'blocking': r'(?i)\b(?:blocking|critical|must\s+fix|blocker)\b',
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

        IMPORTANT: Checks patterns in a specific order to prioritize:
        1. Explicit status declarations (e.g., "Status: APPROVED")
        2. APPROVED indicators (including resolved issues)
        3. BLOCKED indicators (only current blocking issues)
        4. CHANGES_REQUESTED indicators

        This ordering prevents false positives where text like "successfully
        addressing all blocking issues" would incorrectly match BLOCKED.
        """
        # First pass: Check for explicit status declarations in all categories
        for status in [ReviewStatus.APPROVED, ReviewStatus.BLOCKED, ReviewStatus.CHANGES_REQUESTED]:
            patterns = self.STATUS_PATTERNS[status]
            # First pattern is always the explicit "Status: <value>" declaration
            if re.search(patterns[0], text):
                logger.info(f"Matched explicit status declaration: {status.value}")
                return status

        # Second pass: Check APPROVED patterns (to catch resolved issues before BLOCKED)
        for pattern in self.STATUS_PATTERNS[ReviewStatus.APPROVED]:
            if re.search(pattern, text):
                logger.info(f"Matched APPROVED pattern: {pattern}")
                return ReviewStatus.APPROVED

        # Third pass: Check BLOCKED patterns (only current issues now)
        for pattern in self.STATUS_PATTERNS[ReviewStatus.BLOCKED]:
            if re.search(pattern, text):
                logger.info(f"Matched BLOCKED pattern: {pattern}")
                return ReviewStatus.BLOCKED

        # Fourth pass: Check CHANGES_REQUESTED patterns
        for pattern in self.STATUS_PATTERNS[ReviewStatus.CHANGES_REQUESTED]:
            if re.search(pattern, text):
                logger.info(f"Matched CHANGES_REQUESTED pattern: {pattern}")
                return ReviewStatus.CHANGES_REQUESTED

        logger.warning("No status pattern matched, returning UNKNOWN")
        return ReviewStatus.UNKNOWN

    def _extract_findings(self, text: str) -> List[ReviewFinding]:
        """Extract review findings from text"""
        findings = []

        # Look for structured findings sections
        # Pattern 1: Markdown headings with "Issues", "Findings", "Problems", etc.
        sections = re.split(r'(?m)^#{1,3}\s+(?:Review\s+)?(?:Issues?|Findings?|Problems?|Concerns?)', text)

        if len(sections) > 1:
            # Parse findings from the issues section
            findings_text = sections[1]
            findings.extend(self._parse_findings_list(findings_text))

        # Pattern 2: Look for bullet points with severity markers anywhere in text
        findings.extend(self._parse_inline_findings(text))

        # Deduplicate findings by message
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

        for line in lines:
            line = line.strip()

            # Check if this is a new finding (starts with bullet point)
            if re.match(r'^[•\-\*]\s+', line):
                # Save previous finding if exists
                if current_finding:
                    findings.append(current_finding)

                # Parse this finding
                finding_text = re.sub(r'^[•\-\*]\s+', '', line)

                # Extract severity
                severity = self._extract_severity(finding_text)

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

            # Check if this is a suggestion for the current finding
            elif current_finding and re.match(r'^\s+(?:💡|Suggestion:|Recommended:)', line, re.IGNORECASE):
                suggestion_text = re.sub(r'^\s+(?:💡|Suggestion:|Recommended:)\s*', '', line, flags=re.IGNORECASE)
                current_finding.suggestion = suggestion_text.strip()

        # Don't forget the last finding
        if current_finding:
            findings.append(current_finding)

        return findings

    def _parse_inline_findings(self, text: str) -> List[ReviewFinding]:
        """Parse findings from inline text (less structured)"""
        findings = []

        # Look for lines with severity markers
        lines = text.split('\n')
        for line in lines:
            # Skip empty lines and headings
            if not line.strip() or re.match(r'^#{1,6}\s', line):
                continue

            # Check if line contains a severity marker
            severity = self._extract_severity(line)

            # Only include if we found a severity marker (indicates a finding)
            if severity in ['blocking', 'high']:  # Only extract high-priority findings
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
        patterns = [
            r'(?i)(?:score|quality|rating):\s*(\d+(?:\.\d+)?)%',  # "Score: 85%"
            r'(?i)(?:score|quality|rating):\s*(\d+(?:\.\d+)?)',   # "Score: 0.85"
            r'(\d+(?:\.\d+)?)\s*/\s*10',                         # "8.5/10"
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = float(match.group(1))
                # Normalize to 0-1 range
                if value > 1:
                    value = value / 100
                return value

        return 0.0

    def _extract_summary(self, text: str) -> str:
        """Extract review summary"""
        # Look for summary section
        summary_match = re.search(
            r'(?m)^#{1,3}\s+(?:Summary|Overall|Conclusion)\s*\n\s*(.+?)(?=\n#{1,3}\s|\Z)',
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
