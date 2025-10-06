import asyncio
from datetime import datetime
from pathlib import Path

async def test_logging_integration():
    """Verify all components create proper logs"""

    # Clear existing logs
    orchestrator_logs = Path("orchestrator_data/logs").glob("*.log")
    for log_file in orchestrator_logs:
        log_file.unlink()

    pipeline_logs = Path("orchestrator_data/state").glob("*.log")
    for log_file in pipeline_logs:
        log_file.unlink()

    # Run a full pipeline execution
    from tests.integration.test_pipeline_integration import test_pipeline_integration
    await test_pipeline_integration()

    # Check orchestrator log files were created
    orchestrator_log_files = list(Path("orchestrator_data/logs").glob("*.log"))
    assert len(orchestrator_log_files) > 0, "No orchestrator log files created"

    # Check pipeline execution log files were created
    pipeline_log_files = list(Path("orchestrator_data/state").glob("*.log"))
    assert len(pipeline_log_files) > 0, "No pipeline execution log files created"

    # Check orchestrator log content has expected entries
    for log_file in orchestrator_log_files:
        with open(log_file) as f:
            content = f.read()
            assert "business_analyst" in content or "pipeline execution" in content, f"Missing expected content in {log_file}"

    # Check pipeline execution log content has expected entries
    for log_file in pipeline_log_files:
        with open(log_file) as f:
            content = f.read()
            assert "pipeline_id" in content, f"Missing pipeline_id in {log_file}"
            assert "business_analyst" in content, f"Missing agent name in {log_file}"

    print("✅ Logging integration test passed")

if __name__ == "__main__":
    asyncio.run(test_logging_integration())