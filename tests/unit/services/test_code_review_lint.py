"""
Unit tests for services/code_review_lint — rules, config, and runner.

Tests:
- LintConfig.from_dict: defaults and overrides
- is_test_file: positive/negative cases
- check_anti_patterns: vacuous assertions, hardcoded waits, swallowed catches, CSS/XPath selectors
- check_selector_registry: missing/present test IDs
- check_generated_types: generated path with/without header, non-generated path
- CodeReviewLintRunner.run: no changed files, violations, missing registry
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestLintConfig:

    def test_defaults(self):
        from services.code_review_lint.config import LintConfig
        cfg = LintConfig()
        assert cfg.anti_patterns is True
        assert cfg.selector_registry_path is None
        assert cfg.generated_types_paths == []
        assert cfg.pre_flight_context_fetch is True
        assert cfg.issue_body_conformance is True

    def test_from_dict_defaults(self):
        from services.code_review_lint.config import LintConfig
        cfg = LintConfig.from_dict({})
        assert cfg.anti_patterns is True
        assert cfg.selector_registry_path is None
        assert cfg.generated_types_paths == []
        assert cfg.pre_flight_context_fetch is True
        assert cfg.issue_body_conformance is True

    def test_from_dict_overrides(self):
        from services.code_review_lint.config import LintConfig
        cfg = LintConfig.from_dict({
            'anti_patterns': False,
            'selector_registry_path': 'src/selectors.ts',
            'generated_types_paths': ['src/generated/', 'types/'],
            'pre_flight_context_fetch': False,
            'issue_body_conformance': False,
        })
        assert cfg.anti_patterns is False
        assert cfg.selector_registry_path == 'src/selectors.ts'
        assert cfg.generated_types_paths == ['src/generated/', 'types/']
        assert cfg.pre_flight_context_fetch is False
        assert cfg.issue_body_conformance is False

    def test_from_dict_partial_overrides(self):
        from services.code_review_lint.config import LintConfig
        cfg = LintConfig.from_dict({'selector_registry_path': 'reg.ts'})
        assert cfg.anti_patterns is True  # default
        assert cfg.selector_registry_path == 'reg.ts'


class TestIsTestFile:

    def test_test_ts(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('src/components/Button.test.ts') is True

    def test_test_tsx(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('src/components/Button.test.tsx') is True

    def test_test_js(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('src/utils.test.js') is True

    def test_spec_ts(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('src/api.spec.ts') is True

    def test_spec_tsx(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('src/pages/Home.spec.tsx') is True

    def test_spec_js(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('helpers.spec.js') is True

    def test_python_test_prefix(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('tests/test_auth.py') is True

    def test_python_test_suffix(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('auth_test.py') is True

    def test_non_test_ts(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('src/components/Button.ts') is False

    def test_non_test_py(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('services/auth.py') is False

    def test_non_test_js(self):
        from services.code_review_lint.rules import is_test_file
        assert is_test_file('src/index.js') is False


class TestCheckAntiPatterns:

    def test_vacuous_assertion_expect_true_toBe_true(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "  expect(true).toBe(true);\n"
        violations = check_anti_patterns('src/foo.test.ts', content)
        assert any(v.rule == 'vacuous_assertion' for v in violations)

    def test_vacuous_assertion_expect_false_toBe_false(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "expect(false).toBe(false);\n"
        violations = check_anti_patterns('src/foo.test.ts', content)
        assert any(v.rule == 'vacuous_assertion' for v in violations)

    def test_vacuous_assertion_or_true(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "expect(value || true).toBeTruthy();\n"
        violations = check_anti_patterns('src/foo.test.ts', content)
        assert any(v.rule == 'vacuous_assertion' for v in violations)

    def test_vacuous_assertion_python_assert_true(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "assert True\n"
        violations = check_anti_patterns('tests/test_foo.py', content)
        assert any(v.rule == 'vacuous_assertion' for v in violations)

    def test_non_vacuous_assertion_not_flagged(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "expect(result).toBe(true);\n"
        violations = check_anti_patterns('src/foo.test.ts', content)
        assert not any(v.rule == 'vacuous_assertion' for v in violations)

    def test_hardcoded_wait_in_test_file(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "await page.waitForTimeout(5000);\n"
        violations = check_anti_patterns('e2e/login.spec.ts', content)
        assert any(v.rule == 'hardcoded_wait' for v in violations)

    def test_waitForTimeout_in_test_file(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "await waitForTimeout(2000);\n"
        violations = check_anti_patterns('tests/e2e.test.ts', content)
        assert any(v.rule == 'hardcoded_wait' for v in violations)

    def test_time_sleep_in_test_file(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "time.sleep(3)\n"
        violations = check_anti_patterns('tests/test_something.py', content)
        assert any(v.rule == 'hardcoded_wait' for v in violations)

    def test_hardcoded_wait_not_flagged_in_non_test_file(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "await page.waitForTimeout(5000);\n"
        violations = check_anti_patterns('src/utils.ts', content)
        assert not any(v.rule == 'hardcoded_wait' for v in violations)

    def test_swallowed_catch_js(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "try {\n  doSomething();\n} catch (e) {\n  // ignore\n}\n"
        violations = check_anti_patterns('src/foo.ts', content)
        assert any(v.rule == 'swallowed_catch' for v in violations)

    def test_swallowed_catch_python_pass(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "try:\n    do_something()\nexcept Exception:\n    pass\n"
        violations = check_anti_patterns('src/foo.py', content)
        assert any(v.rule == 'swallowed_catch' for v in violations)

    def test_non_empty_catch_not_flagged(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "try {\n  doSomething();\n} catch (e) {\n  logger.error(e);\n}\n"
        violations = check_anti_patterns('src/foo.ts', content)
        assert not any(v.rule == 'swallowed_catch' for v in violations)

    def test_css_selector_in_test_file(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "const el = page.locator('.my-class');\n"
        violations = check_anti_patterns('src/foo.spec.ts', content)
        assert any(v.rule == 'css_xpath_selector' for v in violations)

    def test_id_selector_in_test_file(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "const el = page.locator('#submit-btn');\n"
        violations = check_anti_patterns('src/foo.test.ts', content)
        assert any(v.rule == 'css_xpath_selector' for v in violations)

    def test_jquery_selector_in_test_file(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "const el = $('.modal');\n"
        violations = check_anti_patterns('e2e/app.spec.js', content)
        assert any(v.rule == 'css_xpath_selector' for v in violations)

    def test_semantic_selector_not_flagged(self):
        from services.code_review_lint.rules import check_anti_patterns
        # getByTestId is a semantic selector and should NOT be flagged
        content = "const el = page.getByTestId('submit-btn');\n"
        violations = check_anti_patterns('src/foo.spec.ts', content)
        assert not any(v.rule == 'css_xpath_selector' for v in violations)

    def test_css_selector_not_flagged_in_non_test_file(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "const el = page.locator('.my-class');\n"
        violations = check_anti_patterns('src/utils.ts', content)
        assert not any(v.rule == 'css_xpath_selector' for v in violations)

    def test_violation_line_number_correct(self):
        from services.code_review_lint.rules import check_anti_patterns
        content = "const a = 1;\n\nexpect(true).toBe(true);\n"
        violations = check_anti_patterns('foo.test.ts', content)
        vacuous = [v for v in violations if v.rule == 'vacuous_assertion']
        assert len(vacuous) == 1
        assert vacuous[0].line == 3

    def test_one_liner_js_catch_with_real_handler_not_flagged(self):
        # Fix #1: catch (e) { handle(e); } on one line must NOT be flagged as swallowed
        from services.code_review_lint.rules import check_anti_patterns
        content = "try { run(); } catch (e) { logger.error(e); }\n"
        violations = check_anti_patterns('src/safe.ts', content)
        assert not any(v.rule == 'swallowed_catch' for v in violations)

    def test_one_liner_js_catch_empty_body_still_flagged(self):
        # Fix #1: empty one-liner catch must still be flagged
        from services.code_review_lint.rules import check_anti_patterns
        content = "try { run(); } catch (e) {}\n"
        violations = check_anti_patterns('src/risky.ts', content)
        assert any(v.rule == 'swallowed_catch' for v in violations)

    def test_one_liner_js_catch_comment_only_body_still_flagged(self):
        # Fix #1: one-liner catch with only a comment is swallowed
        from services.code_review_lint.rules import check_anti_patterns
        content = "try { run(); } catch (e) { // ignore }\n"
        violations = check_anti_patterns('src/risky.ts', content)
        assert any(v.rule == 'swallowed_catch' for v in violations)

    def test_swallowed_catch_python_comment_only_body_flagged(self):
        # A Python except block with only comments is a swallowed exception
        from services.code_review_lint.rules import check_anti_patterns
        content = "try:\n    do_something()\nexcept Exception:\n    # ignore errors\n"
        violations = check_anti_patterns('src/risky.py', content)
        assert any(v.rule == 'swallowed_catch' for v in violations)


class TestCheckSelectorRegistry:

    def test_missing_testid_flagged(self):
        from services.code_review_lint.rules import check_selector_registry
        content = 'const el = getByTestId("submit-btn");\n'
        registry = "# selectors\nlogin-btn\ncancel-btn\n"
        violations = check_selector_registry('src/foo.test.ts', content, registry)
        assert any(v.rule == 'selector_registry_sync' for v in violations)
        assert any('submit-btn' in v.detail for v in violations)

    def test_present_testid_not_flagged(self):
        from services.code_review_lint.rules import check_selector_registry
        content = 'const el = getByTestId("submit-btn");\n'
        registry = "submit-btn\nlogin-btn\n"
        violations = check_selector_registry('src/foo.test.ts', content, registry)
        assert not violations

    def test_data_testid_attribute_detected(self):
        from services.code_review_lint.rules import check_selector_registry
        content = '<button data-testid="my-button">Click</button>\n'
        registry = "other-element\n"
        violations = check_selector_registry('src/Comp.tsx', content, registry)
        assert any('my-button' in v.detail for v in violations)

    def test_data_testid_attribute_present_not_flagged(self):
        from services.code_review_lint.rules import check_selector_registry
        content = '<button data-testid="my-button">Click</button>\n'
        registry = "my-button\nother-element\n"
        violations = check_selector_registry('src/Comp.tsx', content, registry)
        assert not violations

    def test_get_by_test_id_variant_detected(self):
        from services.code_review_lint.rules import check_selector_registry
        content = 'page.getByTestId("missing-id");\n'
        registry = "present-id\n"
        violations = check_selector_registry('foo.spec.ts', content, registry)
        assert any('missing-id' in v.detail for v in violations)

    def test_exact_id_not_substring_match(self):
        # Fix #2: 'submit' must NOT pass just because registry contains 'submit-button'
        from services.code_review_lint.rules import check_selector_registry
        content = 'getByTestId("submit");\n'
        registry = "submit-button\n"
        violations = check_selector_registry('foo.test.ts', content, registry)
        assert any(v.rule == 'selector_registry_sync' for v in violations), (
            "'submit' should be flagged missing — 'submit-button' in registry is not a match"
        )

    def test_longer_id_not_matched_by_shorter(self):
        # Fix #2: 'submit-btn-wrapper' must NOT be found when only 'submit-btn' is in registry
        from services.code_review_lint.rules import check_selector_registry
        content = 'getByTestId("submit-btn-wrapper");\n'
        registry = "submit-btn\n"
        violations = check_selector_registry('foo.test.ts', content, registry)
        assert any(v.rule == 'selector_registry_sync' for v in violations)

    def test_exact_id_present_not_flagged_with_similar_entries(self):
        # Fix #2: exact match must pass even when registry has similar entries
        from services.code_review_lint.rules import check_selector_registry
        content = 'getByTestId("submit-btn");\n'
        registry = "submit-btn\nsubmit-btn-wrapper\n"
        violations = check_selector_registry('foo.test.ts', content, registry)
        assert not violations


class TestCheckGeneratedTypes:

    def test_file_in_generated_path_without_header_flagged(self):
        from services.code_review_lint.rules import check_generated_types
        content = "export type Foo = { id: number; };\n"
        violations = check_generated_types('src/generated/types.ts', content, ['src/generated/'])
        assert any(v.rule == 'generated_type_hand_written' for v in violations)

    def test_file_in_generated_path_with_auto_generated_header_not_flagged(self):
        from services.code_review_lint.rules import check_generated_types
        content = "// auto-generated — do not edit\nexport type Foo = { id: number; };\n"
        violations = check_generated_types('src/generated/types.ts', content, ['src/generated/'])
        assert not violations

    def test_file_in_generated_path_with_generated_by_header_not_flagged(self):
        from services.code_review_lint.rules import check_generated_types
        content = "// Generated by graphql-codegen\nexport type Foo = { id: number; };\n"
        violations = check_generated_types('src/generated/types.ts', content, ['src/generated/'])
        assert not violations

    def test_file_in_generated_path_with_at_generated_header_not_flagged(self):
        from services.code_review_lint.rules import check_generated_types
        content = "/* @generated */\nexport type Foo = string;\n"
        violations = check_generated_types('types/api.ts', content, ['types/'])
        assert not violations

    def test_file_outside_generated_path_not_flagged(self):
        from services.code_review_lint.rules import check_generated_types
        content = "export type Foo = { id: number; };\n"  # no header
        violations = check_generated_types('src/components/Button.ts', content, ['src/generated/'])
        assert not violations

    def test_multiple_generated_paths(self):
        from services.code_review_lint.rules import check_generated_types
        content = "export type Bar = string;\n"
        violations = check_generated_types('types/api.ts', content, ['src/generated/', 'types/'])
        assert any(v.rule == 'generated_type_hand_written' for v in violations)

    def test_empty_generated_paths_never_flags(self):
        from services.code_review_lint.rules import check_generated_types
        content = "export type Bar = string;\n"
        violations = check_generated_types('src/generated/foo.ts', content, [])
        assert not violations


class TestCodeReviewLintRunner:

    def test_no_changed_files_passes(self, tmp_path):
        from services.code_review_lint.runner import CodeReviewLintRunner, LintResult
        from services.code_review_lint.config import LintConfig

        runner = CodeReviewLintRunner(lint_config=LintConfig(), project_dir=str(tmp_path))
        with patch.object(runner, '_get_changed_files', return_value=[]):
            result = runner.run(base_commit='HEAD~1')
        assert result.passed is True
        assert result.failure is None

    def test_changed_file_with_violation_fails(self, tmp_path):
        from services.code_review_lint.runner import CodeReviewLintRunner
        from services.code_review_lint.config import LintConfig

        # Create a test file with a vacuous assertion
        test_file = tmp_path / 'foo.test.ts'
        test_file.write_text("expect(true).toBe(true);\n")

        runner = CodeReviewLintRunner(lint_config=LintConfig(), project_dir=str(tmp_path))
        with patch.object(runner, '_get_changed_files', return_value=['foo.test.ts']):
            result = runner.run(base_commit='HEAD~1')

        assert result.passed is False
        assert result.failure is not None
        assert len(result.failure.violations) > 0

    def test_changed_file_without_violation_passes(self, tmp_path):
        from services.code_review_lint.runner import CodeReviewLintRunner
        from services.code_review_lint.config import LintConfig

        clean_file = tmp_path / 'clean.ts'
        clean_file.write_text("export const add = (a: number, b: number) => a + b;\n")

        runner = CodeReviewLintRunner(lint_config=LintConfig(), project_dir=str(tmp_path))
        with patch.object(runner, '_get_changed_files', return_value=['clean.ts']):
            result = runner.run(base_commit='HEAD~1')

        assert result.passed is True

    def test_missing_selector_registry_skips_check(self, tmp_path):
        from services.code_review_lint.runner import CodeReviewLintRunner
        from services.code_review_lint.config import LintConfig

        # File references a test-id, but registry path doesn't exist
        test_file = tmp_path / 'foo.test.ts'
        test_file.write_text('page.getByTestId("missing-id");\n')

        cfg = LintConfig(selector_registry_path='nonexistent/registry.ts')
        runner = CodeReviewLintRunner(lint_config=cfg, project_dir=str(tmp_path))
        with patch.object(runner, '_get_changed_files', return_value=['foo.test.ts']):
            result = runner.run(base_commit='HEAD~1')

        # No selector violations because registry was not found — check is skipped
        if result.failure:
            registry_violations = [v for v in result.failure.violations if v.rule == 'selector_registry_sync']
            assert not registry_violations

    def test_anti_patterns_disabled(self, tmp_path):
        from services.code_review_lint.runner import CodeReviewLintRunner
        from services.code_review_lint.config import LintConfig

        test_file = tmp_path / 'foo.test.ts'
        test_file.write_text("expect(true).toBe(true);\n")

        cfg = LintConfig(anti_patterns=False)
        runner = CodeReviewLintRunner(lint_config=cfg, project_dir=str(tmp_path))
        with patch.object(runner, '_get_changed_files', return_value=['foo.test.ts']):
            result = runner.run(base_commit='HEAD~1')

        assert result.passed is True  # anti-patterns disabled, no violations


class TestLintFailureFormatComment:

    def test_format_comment_groups_by_rule(self):
        from services.code_review_lint.runner import LintFailure
        from services.code_review_lint.rules import LintViolation

        violations = [
            LintViolation(rule='vacuous_assertion', file='foo.test.ts', line=3, detail='Vacuous'),
            LintViolation(rule='hardcoded_wait', file='bar.spec.ts', line=10, detail='Wait'),
            LintViolation(rule='vacuous_assertion', file='baz.test.ts', line=1, detail='Also vacuous'),
        ]
        failure = LintFailure(violations=violations)
        comment = failure.format_comment()

        assert '## Mechanical Lint Failures' in comment
        assert 'Vacuous Assertions' in comment
        assert 'Hardcoded Waits' in comment
        assert 'foo.test.ts:3' in comment
        assert 'bar.spec.ts:10' in comment
        assert 'baz.test.ts:1' in comment

    def test_format_comment_uses_rule_titles(self):
        from services.code_review_lint.runner import LintFailure, RULE_TITLES
        from services.code_review_lint.rules import LintViolation

        for rule, title in RULE_TITLES.items():
            failure = LintFailure(violations=[
                LintViolation(rule=rule, file='x.ts', line=1, detail='detail')
            ])
            comment = failure.format_comment()
            assert title in comment
