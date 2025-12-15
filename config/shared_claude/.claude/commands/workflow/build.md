---
description: Build the project for production
allowed-tools: Bash(npm:*), Bash(python:*), Bash(docker:*), Bash(make:*), Bash(pip:*), Bash(find:*)
argument-hint: [target]
---

# Build Project

## Project Type Detection

!`if [ -f "package.json" ]; then echo "Node.js"; elif [ -f "setup.py" ] || [ -f "pyproject.toml" ]; then echo "Python"; elif [ -f "Dockerfile" ]; then echo "Docker"; elif [ -f "Makefile" ]; then echo "Make"; else echo "Unknown"; fi`

## Build Configuration

!`if [ -f "package.json" ]; then cat package.json | grep -A 5 "scripts"; elif [ -f "pyproject.toml" ]; then cat pyproject.toml | grep -A 5 "build"; elif [ -f "Makefile" ]; then grep "^build:" Makefile; fi`

## Task

Build the project for: $ARGUMENTS

**Instructions:**
1. Detect the build system
2. Run appropriate build command:
   - **Node.js**: `npm run build` or `npm run build:production`
   - **Python**: `python -m build` or `pip install -e .`
   - **Docker**: `docker build -t <image>:<tag> .`
   - **Make**: `make build`
3. Verify build artifacts are created
4. Report build time and output size
5. Check for any warnings or errors

**Build targets:**
- If no argument: Build for production
- `dev`: Development build
- `prod` or `production`: Production build (optimized)
- `docker`: Build Docker image
- `test`: Build for testing

Clean build artifacts first if needed, then run the build.
