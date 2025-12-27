import sys
import os
import logging
from config.manager import config_manager

# Setup logging
logging.basicConfig(level=logging.INFO)

try:
    # Load agent config
    agent_name = "senior_software_engineer"
    agent_config = config_manager.get_agent(agent_name)
    
    if agent_config:
        print(f"Agent: {agent_name}")
        print(f"Retries: {agent_config.retries}")
        print(f"Timeout: {agent_config.timeout}")
        print(f"Model: {agent_config.model}")
    else:
        print(f"Agent {agent_name} not found in config.")

except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
