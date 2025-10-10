import asyncio
import pytest
from datetime import datetime
from agents.business_analyst_agent import BusinessAnalystAgent

import logging

logger = logging.getLogger(__name__)
@pytest.mark.asyncio
async def test_business_analyst_direct():
    """Test Business Analyst agent directly"""

    logger.info(" Testing Business Analyst Agent Directly...")

    agent = BusinessAnalystAgent()

    test_context = {
        'pipeline_id': 'test_direct_001',
        'task_id': 'test_task_001',
        'agent': 'business_analyst',
        'project': 'test_project',
        'context': {
            'issue': {
                'title': 'User Authentication System',
                'body': 'Need secure login/logout functionality with password reset',
                'labels': ['security', 'authentication', 'feature']
            }
        }
    }

    try:
        result = await agent.execute(test_context)
        logger.info(" Business Analyst executed successfully")
        logger.info(f"📄 Result keys: {list(result.keys())}")

        # Validate result structure
        assert 'requirements_analysis' in result
        assert 'quality_metrics' in result

        logger.info(" Result structure validated")
        return True
    except Exception as e:
        logger.info(f" Business Analyst test failed: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_business_analyst_direct())
    exit(0 if success else 1)