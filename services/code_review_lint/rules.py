import re
from typing import List, NamedTuple


class LintViolation(NamedTuple):
    rule: str
    file: str
    line: int
    detail: str


def is_test_file(path: str) -> bool:
    """Return True if the file is a test file by name convention."""
    import os
    basename = os.path.basename(path)
    test_extensions = (
        '.test.ts', '.test.tsx', '.test.js',
        '.spec.ts', '.spec.tsx', '.spec.js',
        '_test.py',
    )
    if any(basename.endswith(ext) for ext in test_extensions):
        return True
    if basename.startswith('test_'):
        return True
    return False


def check_anti_patterns(file_path: str, content: str) -> List[LintViolation]:
    """
    Detect common anti-patterns in source files.

    Checks:
    - Vacuous boolean assertions
    - Hardcoded waits in test files
    - Swallowed catches (empty catch/except blocks)
    - CSS/XPath selectors in test files
    """
    violations: List[LintViolation] = []
    lines = content.splitlines()

    # --- Vacuous boolean assertions ---
    # Matches: expect(true).toBe(true), expect(false).toBe(false), expect(x || true)...
    # Also: bare `assert True` (Python)
    vacuous_patterns = [
        re.compile(r'expect\s*\(\s*true\s*\)\s*\.toBe\s*\(\s*true\s*\)', re.IGNORECASE),
        re.compile(r'expect\s*\(\s*false\s*\)\s*\.toBe\s*\(\s*false\s*\)', re.IGNORECASE),
        re.compile(r'expect\s*\([^)]*\|\|\s*true\s*\)', re.IGNORECASE),
        re.compile(r'^\s*assert\s+True\s*$'),
    ]
    for i, line in enumerate(lines, start=1):
        for pat in vacuous_patterns:
            if pat.search(line):
                violations.append(LintViolation(
                    rule='vacuous_assertion',
                    file=file_path,
                    line=i,
                    detail=f'Vacuous assertion detected: {line.strip()!r}',
                ))
                break  # one violation per line per rule

    # --- Hardcoded waits (test files only) ---
    if is_test_file(file_path):
        wait_patterns = [
            re.compile(r'waitForTimeout\s*\(\s*\d+\s*\)'),
            re.compile(r'time\.sleep\s*\(\s*\d+'),
            re.compile(r'page\.waitForTimeout\s*\('),
        ]
        for i, line in enumerate(lines, start=1):
            for pat in wait_patterns:
                if pat.search(line):
                    violations.append(LintViolation(
                        rule='hardcoded_wait',
                        file=file_path,
                        line=i,
                        detail=f'Hardcoded wait detected in test file: {line.strip()!r}',
                    ))
                    break

    # --- Swallowed catches (multi-line scan) ---
    # Python: bare `except ...:` followed by only pass/comment lines
    # JS/TS: catch(...) { } with only comment body
    _check_swallowed_catches(file_path, content, lines, violations)

    # --- CSS/XPath selectors in test files ---
    if is_test_file(file_path):
        selector_patterns = [
            re.compile(r"\$\s*\(\s*['\"]\."),                           # $('.class')
            re.compile(r"page\.locator\s*\(\s*['\"][.#]"),              # page.locator('.cls') or page.locator('#id')
            re.compile(r"xpath\s*=\s*['\"]//"),                         # xpath='//...'
            re.compile(r"\.querySelector\s*\(\s*['\"][.#]"),            # .querySelector('.cls') or .querySelector('#id')
        ]
        for i, line in enumerate(lines, start=1):
            for pat in selector_patterns:
                if pat.search(line):
                    violations.append(LintViolation(
                        rule='css_xpath_selector',
                        file=file_path,
                        line=i,
                        detail=f'CSS/XPath selector in test file: {line.strip()!r}',
                    ))
                    break

    return violations


def _check_swallowed_catches(
    file_path: str,
    content: str,
    lines: List[str],
    violations: List[LintViolation],
) -> None:
    """Detect empty catch/except blocks (swallowed exceptions)."""
    # Python: `except` followed by only pass or comments
    python_except = re.compile(r'^\s*except\b')
    # JS/TS: catch(...) {
    js_catch = re.compile(r'\bcatch\s*\([^)]*\)\s*\{')

    i = 0
    while i < len(lines):
        line = lines[i]
        # Python except block
        if python_except.match(line):
            # Look at following lines for the block body
            body_lines = []
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                stripped = next_line.strip()
                # If this line has less or equal indent than the except line, block ends
                if stripped and not next_line[0:1] == ' ' and not next_line[0:1] == '\t':
                    break
                indent_except = len(line) - len(line.lstrip())
                indent_next = len(next_line) - len(next_line.lstrip())
                if stripped and indent_next <= indent_except:
                    break
                body_lines.append(stripped)
                j += 1
            # Filter out blank lines and pure comment lines
            meaningful = [
                bl for bl in body_lines
                if bl and not bl.startswith('#')
            ]
            if not meaningful or all(bl == 'pass' for bl in meaningful):
                violations.append(LintViolation(
                    rule='swallowed_catch',
                    file=file_path,
                    line=i + 1,
                    detail=f'Swallowed exception handler (empty except block): {line.strip()!r}',
                ))
        # JS/TS catch block
        elif js_catch.search(line):
            # Find matching closing brace — count only braces from the catch's opening {
            # (The line may start with "} catch (e) {" where the first "}" closes the try block)
            catch_open_pos = line.rfind('{')
            if catch_open_pos == -1:
                i += 1
                continue
            # Count net braces starting from the catch-opening brace
            after_catch = line[catch_open_pos:]
            open_braces = after_catch.count('{') - after_catch.count('}')
            body_lines = []
            if open_braces == 0:
                # Single-line catch: body is between { and the matching } on this line
                close_pos = after_catch.rfind('}')
                inner = after_catch[1:close_pos].strip() if close_pos > 0 else ''
                body_lines = [inner] if inner else []
            else:
                j = i + 1
                while j < len(lines) and open_braces > 0:
                    next_line = lines[j]
                    open_braces += next_line.count('{') - next_line.count('}')
                    body_lines.append(next_line.strip())
                    j += 1
                # Remove the closing brace line
                if body_lines and body_lines[-1] == '}':
                    body_lines = body_lines[:-1]
            meaningful = [
                bl for bl in body_lines
                if bl and not bl.startswith('//') and not bl.startswith('*') and not bl.startswith('/*')
            ]
            if not meaningful:
                violations.append(LintViolation(
                    rule='swallowed_catch',
                    file=file_path,
                    line=i + 1,
                    detail=f'Swallowed exception handler (empty catch block): {line.strip()!r}',
                ))
        i += 1


def check_selector_registry(
    file_path: str,
    content: str,
    registry_content: str,
) -> List[LintViolation]:
    """
    Check that any data-testid or getByTestId values in content exist in the registry.
    """
    violations: List[LintViolation] = []
    lines = content.splitlines()

    # Patterns that capture the test-id value
    patterns = [
        re.compile(r'data-testid=["\']([^"\']+)["\']'),
        re.compile(r'getByTestId\s*\(\s*["\']([^"\']+)["\']'),
    ]

    for i, line in enumerate(lines, start=1):
        for pat in patterns:
            for match in pat.finditer(line):
                test_id = match.group(1)
                # Use boundary matching to avoid 'submit' passing because registry
                # contains 'submit-button' (plain substring check is a false negative).
                boundary_pat = re.compile(
                    r'(?<![a-zA-Z0-9_-])' + re.escape(test_id) + r'(?![a-zA-Z0-9_-])'
                )
                if not boundary_pat.search(registry_content):
                    violations.append(LintViolation(
                        rule='selector_registry_sync',
                        file=file_path,
                        line=i,
                        detail=f'Test ID {test_id!r} not found in selector registry',
                    ))

    return violations


def check_generated_types(
    file_path: str,
    content: str,
    generated_paths: List[str],
) -> List[LintViolation]:
    """
    If file_path starts with any path in generated_paths, check for auto-generation header.
    """
    violations: List[LintViolation] = []

    is_generated_path = any(file_path.startswith(p) for p in generated_paths)
    if not is_generated_path:
        return violations

    header_region = content[:500].lower()
    auto_gen_markers = [
        'auto-generated',
        'do not edit',
        'generated by',
        '@generated',
        'this file is generated',
    ]
    has_header = any(marker in header_region for marker in auto_gen_markers)
    if not has_header:
        violations.append(LintViolation(
            rule='generated_type_hand_written',
            file=file_path,
            line=1,
            detail=f'File is in a generated-types path but lacks an auto-generation header comment',
        ))

    return violations
