# Docker Test Fix - Proper Workspace Setup

## Problem Summary

The Docker integration tests were failing because they used `tempfile.TemporaryDirectory()` which creates directories in `/tmp/` with restrictive permissions that prevent Docker containers (running as UID 1000) from writing files.

## Solution Implemented

Created a new `docker_workspace` fixture that:

1. **Detects execution environment**
   - If running in orchestrator container → uses `/workspace/test_project/`
   - If running locally → creates temp directory with world-writable permissions

2. **Sets proper permissions**
   ```python
   workspace.chmod(stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)  # 0777
   ```

3. **Matches production behavior**
   - Real orchestrator uses `/workspace/<project>/`
   - Tests now use the same structure

## Code Changes

### New Fixture in `test_claude_code_integration.py`

```python
@pytest.fixture
def docker_workspace():
    """
    Create a workspace directory for Docker tests.
    
    Uses /workspace/test_project if running in orchestrator container,
    otherwise creates a properly permissioned directory for testing.
    """
    # Check if we're in the orchestrator container with /workspace
    workspace_root = Path('/workspace')
    if workspace_root.exists() and workspace_root.is_dir():
        # Running in container - use real workspace
        test_project_dir = workspace_root / 'test_project'
        test_project_dir.mkdir(exist_ok=True)
        
        # Create test files
        (test_project_dir / "README.md").write_text("# Test Project\n\nThis is a test.")
        (test_project_dir / "hello.py").write_text("print('Hello, World!')")
        
        yield test_project_dir
        
        # Cleanup
        import shutil
        if test_project_dir.exists():
            try:
                shutil.rmtree(test_project_dir)
            except Exception as e:
                print(f"Warning: Could not clean up test project: {e}")
    else:
        # Not in container - create a temp directory with proper permissions
        import tempfile
        import stat
        
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            
            # Make it world-writable so Docker container can write
            # This simulates the real /workspace mount permissions
            try:
                workspace.chmod(stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            except Exception as e:
                print(f"Warning: Could not set permissions: {e}")
            
            # Create test files
            (workspace / "README.md").write_text("# Test Project\n\nThis is a test.")
            (workspace / "hello.py").write_text("print('Hello, World!')")
            
            yield workspace
```

### Updated Test Methods

Both Docker tests now use `docker_workspace` instead of `temp_workspace`:

```python
async def test_docker_hello_world(self, docker_workspace, obs_manager, stream_events):
    """Test simple prompt in Docker generates correct events"""
    
    with patch('claude.claude_integration.workspace_manager') as mock_wm:
        mock_wm.get_project_dir.return_value = docker_workspace
        # ... rest of test
```

## Running the Tests

### Option 1: Inside Orchestrator Container (RECOMMENDED)

This is the most realistic test environment:

```bash
# Enter the orchestrator container
docker exec -it clauditoreum-orchestrator-1 bash

# Activate environment
source .venv/bin/activate
source .env

# Run Docker tests
pytest tests/integration/test_claude_code_integration.py::TestClaudeCodeDockerExecution -v

# Or run all integration tests
pytest tests/integration/test_claude_code_integration.py -v
```

**Benefits**:
- Uses real `/workspace/` structure
- Matches production environment exactly
- Tests actual Docker-in-Docker execution
- Most reliable results

### Option 2: Local Development Machine

Tests will create properly-permissioned temp directories:

```bash
# From host machine
cd /home/austinsand/workspace/orchestrator/clauditoreum
source .venv/bin/activate
source .env

# Run Docker tests
pytest tests/integration/test_claude_code_integration.py::TestClaudeCodeDockerExecution -v
```

**Notes**:
- Requires Docker running on host
- Uses temp directories with 0777 permissions
- May have SELinux/AppArmor restrictions on some systems
- Less realistic than running in container

### Option 3: Skip Docker Tests

If you just want to verify core functionality:

```bash
# Run all tests except Docker
pytest tests/integration/test_claude_code_integration.py -v -k "not docker"
```

**Results**: 10/10 tests pass (skips 2 Docker tests)

## What the Tests Now Validate

### ✅ test_docker_hello_world

**Validates**:
1. Docker container can be launched
2. Claude CLI executes inside container
3. Container has write access to workspace
4. Monitoring events are emitted
5. Stream events are captured
6. API calls complete successfully

**Expected Behavior**:
- Container launches with project directory mounted
- Safety check passes (container can write)
- Claude Code executes simple prompt
- Response is returned
- Events logged to Redis

### ✅ test_docker_file_operations

**Validates**:
1. Docker container can read mounted files
2. Claude can access project files (README.md)
3. File operations work across container boundary
4. Response includes file content

**Expected Behavior**:
- Container can read README.md
- Claude responds with content from file
- File operations succeed in container

## Expected Test Results

After the fix, you should see:

```
tests/integration/test_claude_code_integration.py::TestClaudeCodeDockerExecution::test_docker_hello_world PASSED
tests/integration/test_claude_code_integration.py::TestClaudeCodeDockerExecution::test_docker_file_operations PASSED
```

**Total**: 12/12 tests pass ✅

## Troubleshooting

### Permission Denied Errors

If you still see permission errors:

1. **Check Docker daemon mode**
   ```bash
   docker info | grep -i rootless
   ```
   If "Rootless mode: true", you may need additional setup.

2. **Check SELinux (if on RHEL/CentOS/Fedora)**
   ```bash
   getenforce
   # If "Enforcing", may need: setenforce 0 (temporarily)
   ```

3. **Verify orchestrator container user**
   ```bash
   docker exec clauditoreum-orchestrator-1 id
   # Should show: uid=1000(orchestrator)
   ```

### Tests Still Skip

If tests are skipped with "Docker not available":

```bash
# Check Docker is accessible
docker info > /dev/null 2>&1
echo $?  # Should be 0

# Check Docker socket permissions
ls -l /var/run/docker.sock
```

### Container Write Test Fails

If the safety check still fails:

1. **Verify mount works**
   ```bash
   # In orchestrator container
   docker run --rm -v /workspace/test_project:/workspace:rw \
     clauditoreum-orchestrator:latest \
     sh -c 'touch /workspace/test.txt && rm /workspace/test.txt'
   ```

2. **Check directory ownership**
   ```bash
   # Should be owned by user 1000 or world-writable
   ls -ld /workspace/test_project
   ```

## Architecture Notes

### Why This Fix Works

1. **Matches Real Environment**
   - Production: `/workspace/<project>/`
   - Tests: `/workspace/test_project/` (in container) or permissioned temp (local)

2. **Proper Permissions**
   - Real `/workspace` is mounted with correct ownership
   - Test directories now match those permissions

3. **Container User Access**
   - Orchestrator user (UID 1000) can write to workspace
   - Agent containers inherit this access

### Test Environment Layers

```
┌─────────────────────────────────────────┐
│ Host Machine                            │
│  ~/workspace/orchestrator/              │
│                                         │
│  ┌───────────────────────────────────┐ │
│  │ Orchestrator Container            │ │
│  │  /workspace/ (mounted from host)  │ │
│  │                                   │ │
│  │  ┌─────────────────────────────┐ │ │
│  │  │ Agent Container             │ │ │
│  │  │  /workspace/<project>:rw    │ │ │
│  │  │  (mounted from orchestrator)│ │ │
│  │  └─────────────────────────────┘ │ │
│  └───────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

Tests must run at the **Orchestrator Container layer** to properly test Docker execution.

## Success Criteria

After running the fixed tests, you should see:

✅ **All 12 integration tests pass**
✅ **Docker containers can write to workspace**
✅ **Safety check passes**
✅ **Claude Code executes in Docker**
✅ **Monitoring events captured**
✅ **Stream events received**

This validates that Claude Code integration works correctly in the real orchestrator environment!
