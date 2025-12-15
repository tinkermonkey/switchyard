---
name: testing-expert
description: Expert in test generation, test patterns, and testing best practices. Use when writing tests, generating test cases, improving test coverage, or debugging test failures. Supports pytest, jest, mocha, and other testing frameworks.
allowed-tools: Read, Grep, Glob, Bash(pytest:*), Bash(npm:*), Bash(find:*)
---

# Testing Expert Skill

Expert in software testing across multiple frameworks and languages.

## Expertise Areas

- **Test Generation**: Create comprehensive test suites from code
- **Test Patterns**: Unit, integration, E2E test patterns
- **Mocking & Fixtures**: Test data and dependency mocking
- **Coverage Analysis**: Identify untested code paths
- **Test Debugging**: Fix failing and flaky tests

## Supported Frameworks

**Python:**
- pytest (fixtures, parametrize, markers)
- unittest (TestCase, mocks)
- doctest (embedded tests)

**JavaScript/TypeScript:**
- Jest (mocks, snapshots, coverage)
- Mocha + Chai (BDD-style tests)
- Vitest (Vite-native testing)

**Test Libraries:**
- pytest-cov, pytest-mock, pytest-asyncio
- @testing-library/react, @testing-library/dom
- sinon (mocks, spies, stubs)

## Test Generation Process

When generating tests:

1. **Analyze the code structure**
   - Identify functions, classes, methods
   - Determine input/output contracts
   - Find edge cases and error conditions

2. **Plan test coverage**
   - Happy path scenarios
   - Edge cases (empty, null, boundary values)
   - Error conditions (exceptions, invalid input)
   - Integration points (dependencies, I/O)

3. **Generate tests following patterns**:

### pytest Pattern

```python
import pytest
from mymodule import MyClass, my_function

class TestMyClass:
    """Test suite for MyClass"""

    @pytest.fixture
    def instance(self):
        """Fixture providing a MyClass instance"""
        return MyClass(param="test")

    def test_happy_path(self, instance):
        """Test normal operation"""
        result = instance.process("input")
        assert result == "expected"

    def test_edge_case_empty(self, instance):
        """Test with empty input"""
        with pytest.raises(ValueError):
            instance.process("")

    @pytest.mark.parametrize("input,expected", [
        ("a", "A"),
        ("b", "B"),
        ("", ValueError),
    ])
    def test_parametrized(self, instance, input, expected):
        """Test multiple scenarios"""
        if isinstance(expected, type) and issubclass(expected, Exception):
            with pytest.raises(expected):
                instance.process(input)
        else:
            assert instance.process(input) == expected
```

### Jest Pattern

```typescript
import { MyClass } from './MyClass';

describe('MyClass', () => {
  let instance: MyClass;

  beforeEach(() => {
    instance = new MyClass('test');
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('should process input correctly', () => {
    const result = instance.process('input');
    expect(result).toBe('expected');
  });

  it('should throw on empty input', () => {
    expect(() => instance.process('')).toThrow(ValueError);
  });

  it.each([
    ['a', 'A'],
    ['b', 'B'],
  ])('should process %s to %s', (input, expected) => {
    expect(instance.process(input)).toBe(expected);
  });
});
```

## Mocking Strategies

**Mock external dependencies:**

```python
# pytest
def test_api_call(mocker):
    mock_response = mocker.Mock()
    mock_response.json.return_value = {'data': 'test'}

    mocker.patch('requests.get', return_value=mock_response)

    result = fetch_data()
    assert result == {'data': 'test'}
```

```typescript
// jest
jest.mock('./api');
import { fetchData } from './api';

it('should call API', async () => {
  (fetchData as jest.Mock).mockResolvedValue({ data: 'test' });

  const result = await getData();
  expect(result).toEqual({ data: 'test' });
});
```

## Test Organization

**Directory structure:**
```
tests/
├── unit/           # Fast, isolated tests
├── integration/    # Tests with dependencies
├── e2e/           # End-to-end tests
├── fixtures/      # Test data and fixtures
└── conftest.py    # pytest configuration
```

**Naming conventions:**
- Test files: `test_*.py` or `*.test.ts`
- Test functions: `test_*` (pytest) or `it('should...')` (jest)
- Test classes: `Test*` or `*Test`

## Coverage Best Practices

**Target coverage:**
- Critical paths: 100%
- Business logic: >90%
- Utilities: >80%
- Overall: >70%

**Coverage gaps to address:**
- Error handling branches
- Edge case validation
- Async/concurrent operations
- External service interactions

## Test Debugging Tips

**Common issues:**
1. **Flaky tests**: Add explicit waits, fix race conditions
2. **Slow tests**: Use mocks, reduce I/O, parallelize
3. **Brittle tests**: Use flexible assertions, avoid implementation details
4. **Hard to test**: Refactor for testability, inject dependencies

## Quick Reference

**Run tests:**
```bash
# pytest
pytest tests/ -v --cov=. --cov-report=term-missing

# jest
npm test -- --coverage --verbose
```

**Run specific tests:**
```bash
# pytest
pytest tests/test_module.py::TestClass::test_method -v

# jest
npm test -- MyComponent.test.ts
```

**Debug tests:**
```bash
# pytest with debugger
pytest --pdb

# jest with node debugger
node --inspect-brk node_modules/.bin/jest --runInBand
```

Use this skill when you need help with testing strategies, test generation, coverage improvement, or debugging test failures.
