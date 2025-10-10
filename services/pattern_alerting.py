"""
Pattern Alerting System

Sends alerts when high-severity patterns are detected
"""

import logging
import json
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class AlertChannel:
    """Base class for alert channels"""

    def send(self, pattern: Dict[str, Any], occurrence: Dict[str, Any]) -> bool:
        """
        Send an alert

        Returns:
            True if sent successfully, False otherwise
        """
        raise NotImplementedError()


class WebhookChannel(AlertChannel):
    """Sends alerts via HTTP webhook"""

    def __init__(self, webhook_url: str, channel_name: str = "webhook"):
        self.webhook_url = webhook_url
        self.channel_name = channel_name

    def send(self, pattern: Dict[str, Any], occurrence: Dict[str, Any]) -> bool:
        """Send alert via webhook"""
        try:
            # Build alert message
            message = self._build_message(pattern, occurrence)

            # Send POST request
            response = requests.post(
                self.webhook_url,
                json=message,
                timeout=10
            )

            if response.status_code == 200:
                logger.info(f"Alert sent successfully to {self.channel_name}")
                return True
            else:
                logger.warning(
                    f"Alert to {self.channel_name} failed: {response.status_code} - {response.text}"
                )
                return False

        except Exception as e:
            logger.error(f"Error sending alert to {self.channel_name}: {e}")
            return False

    def _build_message(self, pattern: Dict[str, Any], occurrence: Dict[str, Any]) -> Dict[str, Any]:
        """Build webhook message payload"""
        return {
            "text": f"⚠️ Pattern Detected: {pattern['pattern_name']}",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"⚠️ Pattern Alert: {pattern['pattern_name']}"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Severity:* {pattern['severity']}"},
                        {"type": "mrkdwn", "text": f"*Category:* {pattern['pattern_category']}"},
                        {"type": "mrkdwn", "text": f"*Agent:* {occurrence.get('agent_name', 'N/A')}"},
                        {"type": "mrkdwn", "text": f"*Project:* {occurrence.get('project', 'N/A')}"}
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Description:* {pattern['description']}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Error:* ```{occurrence.get('error_message', 'N/A')[:200]}```"
                    }
                }
            ]
        }


class SlackChannel(WebhookChannel):
    """Sends alerts to Slack via webhook"""

    def __init__(self, webhook_url: str):
        super().__init__(webhook_url, "Slack")


class DiscordChannel(WebhookChannel):
    """Sends alerts to Discord via webhook"""

    def __init__(self, webhook_url: str):
        super().__init__(webhook_url, "Discord")

    def _build_message(self, pattern: Dict[str, Any], occurrence: Dict[str, Any]) -> Dict[str, Any]:
        """Build Discord-specific message payload"""
        # Discord uses a different format
        color_map = {
            "critical": 0xFF0000,  # Red
            "high": 0xFF6600,      # Orange
            "medium": 0xFFCC00,    # Yellow
            "low": 0x00FF00        # Green
        }

        return {
            "embeds": [{
                "title": f"⚠️ Pattern Detected: {pattern['pattern_name']}",
                "description": pattern['description'],
                "color": color_map.get(pattern['severity'], 0x808080),
                "fields": [
                    {"name": "Severity", "value": pattern['severity'], "inline": True},
                    {"name": "Category", "value": pattern['pattern_category'], "inline": True},
                    {"name": "Agent", "value": occurrence.get('agent_name', 'N/A'), "inline": True},
                    {"name": "Project", "value": occurrence.get('project', 'N/A'), "inline": True},
                    {"name": "Error", "value": f"```{occurrence.get('error_message', 'N/A')[:200]}```", "inline": False}
                ],
                "timestamp": datetime.utcnow().isoformat() + 'Z'
            }]
        }


class LogChannel(AlertChannel):
    """Logs alerts to console/file (for development)"""

    def send(self, pattern: Dict[str, Any], occurrence: Dict[str, Any]) -> bool:
        """Log alert to console"""
        logger.warning(
            f"PATTERN ALERT: {pattern['pattern_name']} (severity={pattern['severity']}) "
            f"- Agent: {occurrence.get('agent_name')}, Project: {occurrence.get('project')}, "
            f"Error: {occurrence.get('error_message', 'N/A')[:100]}"
        )
        return True


class PatternAlerter:
    """Manages alert channels and routing"""

    def __init__(self):
        self.channels: List[AlertChannel] = []
        self.severity_thresholds = {
            "critical": ["critical"],
            "high": ["critical", "high"],
            "medium": ["critical", "high", "medium"],
            "low": ["critical", "high", "medium", "low"]
        }
        self.min_severity = "high"  # Only alert on high+ severity by default

    def add_channel(self, channel: AlertChannel):
        """Add an alert channel"""
        self.channels.append(channel)

    def set_min_severity(self, severity: str):
        """Set minimum severity level for alerts"""
        if severity not in ["critical", "high", "medium", "low"]:
            raise ValueError(f"Invalid severity: {severity}")
        self.min_severity = severity

    def should_alert(self, severity: str) -> bool:
        """Check if pattern severity warrants an alert"""
        allowed_severities = self.severity_thresholds.get(self.min_severity, [])
        return severity in allowed_severities

    def send_alert(
        self,
        pattern: Dict[str, Any],
        occurrence: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Send alert through configured channels

        Args:
            pattern: Pattern information
            occurrence: Occurrence information

        Returns:
            Alert status dict
        """
        severity = pattern.get('severity', 'medium')

        # Check if we should alert
        if not self.should_alert(severity):
            return {
                "sent": False,
                "reason": f"Severity '{severity}' below threshold '{self.min_severity}'"
            }

        # Send to all channels
        results = []
        for channel in self.channels:
            try:
                success = channel.send(pattern, occurrence)
                results.append({
                    "channel": channel.__class__.__name__,
                    "success": success
                })
            except Exception as e:
                logger.error(f"Error sending alert via {channel.__class__.__name__}: {e}")
                results.append({
                    "channel": channel.__class__.__name__,
                    "success": False,
                    "error": str(e)
                })

        return {
            "sent": True,
            "results": results,
            "channels_succeeded": sum(1 for r in results if r.get('success')),
            "channels_failed": sum(1 for r in results if not r.get('success'))
        }


def create_alerter_from_config(config: Dict[str, Any]) -> PatternAlerter:
    """
    Create PatternAlerter from configuration

    Args:
        config: Configuration dict with alert settings

    Returns:
        Configured PatternAlerter
    """
    alerter = PatternAlerter()

    # Set minimum severity
    min_severity = config.get('min_severity', 'high')
    alerter.set_min_severity(min_severity)

    # Add channels from config
    channels_config = config.get('channels', [])

    for channel_config in channels_config:
        channel_type = channel_config.get('type')
        enabled = channel_config.get('enabled', True)

        if not enabled:
            continue

        if channel_type == 'slack':
            webhook_url = channel_config.get('webhook_url')
            if webhook_url:
                alerter.add_channel(SlackChannel(webhook_url))
                logger.info("Added Slack alert channel")

        elif channel_type == 'discord':
            webhook_url = channel_config.get('webhook_url')
            if webhook_url:
                alerter.add_channel(DiscordChannel(webhook_url))
                logger.info("Added Discord alert channel")

        elif channel_type == 'log':
            alerter.add_channel(LogChannel())
            logger.info("Added Log alert channel")

    # If no channels configured, add log channel as fallback
    if not alerter.channels:
        alerter.add_channel(LogChannel())
        logger.info("No alert channels configured, using Log channel")

    return alerter
