---
invoked_by: services/review_pattern_detector.py — _analyze_pattern_with_llm() via default_loader.workflow_template("analysis/ignored_review_pattern")
  Called as: loader.workflow_template("analysis/ignored_review_pattern").format(
    agent=agent, category=category, severity=severity,
    ignore_rate=f"{ignore_rate:.1%}", sample_size=len(sample_findings), examples_text=examples_text)
variables:
  agent: Review agent name
  category: Finding category
  severity: Finding severity
  ignore_rate: Pre-formatted percentage string (e.g. "73.5%")
  sample_size: Number of sample findings examined
  examples_text: Pre-formatted numbered list of example ignored messages
notes: >
  The JSON schema block uses {{ }} for literal braces (escaped for str.format()).
---

Analyze these review comments that were frequently IGNORED by developers:

Agent: {agent}
Category: {category}
Severity: {severity}
Ignore Rate: {ignore_rate}
Sample Size: {sample_size}

Examples of ignored feedback:
{examples_text}

Tasks:
1. Identify the common pattern across these ignored review comments
2. Explain why developers might be ignoring this type of feedback
3. Suggest what action should be taken (suppress, adjust_severity, or context_filter)

Return ONLY valid JSON in this exact format:
{{
  "pattern_description": "Brief description of the common pattern (1-2 sentences)",
  "reason_ignored": "Why developers ignore this feedback (1-2 sentences)",
  "suggested_action": "suppress|adjust_severity|context_filter",
  "confidence": 0.0-1.0
}}
