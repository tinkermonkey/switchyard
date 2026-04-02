"""
Unit tests for the prompts package.

Verifies that PromptBuilder assembles the correct content for every agent
workflow — correct guidelines, quality standards, output instructions,
review cycle blocks, and placeholder expansion.
"""

import pytest
from prompts import PromptBuilder, PromptContext
from prompts.context import IssueContext, ReviewCycleContext
from prompts.loader import ContentLoader, _strip_frontmatter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_task(extras=None):
    """Minimal task_context for initial mode."""
    ctx = {"issue": {"title": "Add search", "body": "Users need search"}, "project": "myapp"}
    if extras:
        ctx.update(extras)
    return ctx


def _make_ctx(agent_name, agent_display_name, agent_role, output_sections, task_extras=None,
              makes_code_changes=False, filesystem_write_allowed=False, prompt_variant="standard",
              include_sub_issue_format=False):
    return PromptContext.from_task_context(
        _base_task(task_extras),
        agent_name=agent_name,
        agent_display_name=agent_display_name,
        agent_role_description=agent_role,
        output_sections=output_sections,
        makes_code_changes=makes_code_changes,
        filesystem_write_allowed=filesystem_write_allowed,
        prompt_variant=prompt_variant,
        include_sub_issue_format=include_sub_issue_format,
    )


BUILDER = PromptBuilder()


# ---------------------------------------------------------------------------
# ContentLoader — all known content files load non-empty
# ---------------------------------------------------------------------------

class TestContentLoader:
    LOADER = ContentLoader()

    @pytest.mark.parametrize("agent_name", [
        "business_analyst", "idea_researcher", "software_architect",
        "senior_software_engineer", "technical_writer", "dev_environment_setup",
        "work_breakdown_agent",
    ])
    def test_guidelines_non_empty(self, agent_name):
        assert self.LOADER.agent_guidelines(agent_name), \
            f"guidelines.md missing for {agent_name}"

    @pytest.mark.parametrize("agent_name", [
        "business_analyst", "idea_researcher", "software_architect",
        "senior_software_engineer", "technical_writer", "work_breakdown_agent",
    ])
    def test_quality_standards_non_empty(self, agent_name):
        assert self.LOADER.agent_quality_standards(agent_name), \
            f"quality_standards.md missing for {agent_name}"

    @pytest.mark.parametrize("agent_name", [
        "code_reviewer", "documentation_editor", "dev_environment_verifier",
    ])
    def test_review_task_non_empty(self, agent_name):
        assert self.LOADER.agent_review_task(agent_name), \
            f"review_task.md missing for {agent_name}"

    @pytest.mark.parametrize("agent_name", ["code_reviewer", "documentation_editor"])
    def test_format_files_non_empty(self, agent_name):
        assert self.LOADER.agent_format_initial(agent_name), \
            f"format_initial.md missing for {agent_name}"
        assert self.LOADER.agent_format_rereviewing(agent_name), \
            f"format_rereviewing.md missing for {agent_name}"

    @pytest.mark.parametrize("workflow_path,label", [
        ("pr_review/code_review", "pr_code_reviewer"),
        ("pr_review/requirements", "requirements_verifier"),
    ])
    def test_pr_review_workflow_templates_non_empty(self, workflow_path, label):
        assert self.LOADER.workflow_template(workflow_path), \
            f"workflows/{workflow_path}.md missing for {label}"

    def test_sub_issue_format_non_empty(self):
        assert self.LOADER.agent_sub_issue_format("work_breakdown_agent")

    @pytest.mark.parametrize("path", [
        "output/code_writing",
        "output/analysis",
        "question/output_code",
        "question/output_analysis",
    ])
    def test_output_workflow_templates_non_empty(self, path):
        assert self.LOADER.workflow_template(path), f"workflows/{path}.md is empty or missing"

    @pytest.mark.parametrize("path", [
        "revision/cycle_context",
        "revision/feedback_context",
        "review/iteration_initial",
        "review/iteration_rereviewing",
        "review/iteration_post_human",
        "verification/iteration_initial",
        "verification/iteration_rereviewing",
    ])
    def test_cycle_workflow_templates_non_empty(self, path):
        assert self.LOADER.workflow_template(path), f"workflows/{path}.md is empty or missing"

    def test_documentation_editor_rereviewing_context_non_empty(self):
        assert self.LOADER.agent_rereviewing_context("documentation_editor"), \
            "documentation_editor/rereviewing_context.md missing"

    def test_agent_rereviewing_context_missing_returns_empty(self):
        assert self.LOADER.agent_rereviewing_context("nonexistent_agent") == ""

    def test_missing_file_returns_empty_string(self):
        assert self.LOADER.agent_guidelines("nonexistent_agent") == ""
        assert self.LOADER.agent_quality_standards("nonexistent_agent") == ""

    @pytest.mark.parametrize("path", [
        "initial/standard", "initial/implementation",
        "question/file_context", "question/embedded",
        "revision/file_based", "revision/embedded",
        "review/prompt", "verification/prompt",
    ])
    def test_structural_workflow_templates_non_empty(self, path):
        assert self.LOADER.workflow_template(path), f"workflows/{path}.md is empty or missing"

    def test_workflow_template_missing_returns_empty(self):
        assert self.LOADER.workflow_template("nonexistent/path") == ""

    def test_workflow_templates_have_no_frontmatter(self):
        """Frontmatter must be stripped before content is returned."""
        for path in ("initial/standard", "initial/implementation",
                     "question/file_context", "question/embedded",
                     "revision/file_based", "revision/embedded",
                     "review/prompt", "verification/prompt"):
            content = self.LOADER.workflow_template(path)
            assert not content.startswith("---"), \
                f"workflows/{path}.md still contains frontmatter after loading"

    def test_strip_frontmatter_removes_yaml_block(self):
        raw = "---\nkey: value\nanother: thing\n---\nActual content here."
        assert _strip_frontmatter(raw) == "Actual content here."

    def test_strip_frontmatter_no_frontmatter_unchanged(self):
        raw = "No frontmatter here.\nJust content."
        assert _strip_frontmatter(raw) == raw

    def test_strip_frontmatter_unclosed_returns_unchanged(self):
        raw = "---\nkey: value\nno closing delimiter"
        assert _strip_frontmatter(raw) == raw

    def test_content_files_have_no_frontmatter_after_load(self):
        """All loaded content should be free of frontmatter delimiters at the start."""
        loader = self.LOADER
        # Sample a cross-section of file types
        assert not loader.agent_guidelines("business_analyst").startswith("---")
        assert not loader.agent_quality_standards("software_architect").startswith("---")
        assert not loader.workflow_template("review/iteration_initial").startswith("---")
        assert not loader.workflow_template("revision/cycle_context").startswith("---")
        assert not loader.workflow_template("output/code_writing").startswith("---")
        assert not loader.agent_review_task("code_reviewer").startswith("---")


# ---------------------------------------------------------------------------
# PromptContext.from_task_context — mode detection
# ---------------------------------------------------------------------------

class TestModeDetection:
    def test_initial_mode_default(self):
        ctx = _make_ctx("business_analyst", "BA", "role", [])
        assert ctx.mode == "initial"

    def test_revision_mode_from_trigger(self):
        ctx = _make_ctx("business_analyst", "BA", "role", [],
                        task_extras={"trigger": "review_cycle_revision",
                                     "review_cycle": {"iteration": 1, "max_iterations": 3}})
        assert ctx.mode == "revision"

    def test_revision_mode_from_feedback_key(self):
        ctx = _make_ctx("business_analyst", "BA", "role", [],
                        task_extras={"feedback": {"formatted_text": "Please revise X"}})
        assert ctx.mode == "revision"

    def test_revision_mode_from_revision_key(self):
        ctx = _make_ctx("business_analyst", "BA", "role", [],
                        task_extras={"revision": {"feedback": "Fix this", "previous_output": "old"}})
        assert ctx.mode == "revision"

    def test_question_mode_requires_all_three_signals(self):
        # Missing thread_history → still revision
        ctx = _make_ctx("business_analyst", "BA", "role", [],
                        task_extras={"trigger": "feedback_loop", "conversation_mode": "threaded"})
        assert ctx.mode != "question"

    def test_question_mode_all_signals(self):
        ctx = _make_ctx("business_analyst", "BA", "role", [],
                        task_extras={
                            "trigger": "feedback_loop",
                            "conversation_mode": "threaded",
                            "thread_history": [{"role": "user", "author": "alice", "body": "question"}],
                            "feedback": {"formatted_text": "question"},
                        })
        assert ctx.mode == "question"

    def test_feedback_loop_without_threaded_mode_is_revision(self):
        ctx = _make_ctx("business_analyst", "BA", "role", [],
                        task_extras={"trigger": "feedback_loop",
                                     "thread_history": [{"role": "user", "author": "a", "body": "q"}]})
        assert ctx.mode == "revision"

    def test_review_cycle_context_populated(self):
        ctx = _make_ctx("business_analyst", "BA", "role", [],
                        task_extras={
                            "trigger": "review_cycle_revision",
                            "review_cycle": {
                                "iteration": 2,
                                "max_iterations": 3,
                                "reviewer_agent": "code_reviewer",
                                "is_rereviewing": True,
                                "previous_review_feedback": "Fix tests",
                            }
                        })
        assert ctx.review_cycle is not None
        assert ctx.review_cycle.iteration == 2
        assert ctx.review_cycle.is_rereviewing is True
        assert ctx.review_cycle.previous_review_feedback == "Fix tests"

    def test_direct_prompt_passthrough(self):
        ctx = _make_ctx("business_analyst", "BA", "role", [],
                        task_extras={"direct_prompt": "do this specific thing"})
        result = BUILDER.build(ctx)
        assert result == "do this specific thing"


# ---------------------------------------------------------------------------
# Output instruction routing
# ---------------------------------------------------------------------------

class TestOutputInstructionRouting:
    def test_analysis_agent_gets_analysis_instructions(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"], makes_code_changes=False)
        prompt = BUILDER.build(ctx)
        assert "DO NOT create any files" in prompt

    def test_code_writing_agent_does_not_get_analysis_restriction(self):
        ctx = _make_ctx("senior_software_engineer", "Senior Software Engineer", "I code.",
                        ["Changes Made"],
                        makes_code_changes=True, filesystem_write_allowed=True,
                        prompt_variant="implementation")
        prompt = BUILDER.build(ctx)
        assert "DO NOT create any files" not in prompt

    def test_filesystem_write_allowed_flag_overrides(self):
        # filesystem_write_allowed alone (without makes_code_changes) should
        # also select code_writing output instructions.
        ctx = _make_ctx("dev_environment_setup", "Dev Env Setup", "I set up envs.",
                        [], filesystem_write_allowed=True)
        prompt = BUILDER.build(ctx)
        assert "DO NOT create any files" not in prompt

    def test_question_mode_analysis_gets_analysis_question_instructions(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.", [],
                        task_extras={
                            "trigger": "feedback_loop",
                            "conversation_mode": "threaded",
                            "thread_history": [{"role": "user", "author": "a", "body": "q"}],
                            "feedback": {"formatted_text": "expand on X"},
                        })
        prompt = BUILDER.build(ctx)
        # analysis_question.md has DO NOT create any files but NOT the "posting" phrasing
        assert "DO NOT create any files" in prompt
        # Should be in question mode
        assert ctx.mode == "question"


# ---------------------------------------------------------------------------
# Per-agent content — guidelines and quality standards appear in prompt
# ---------------------------------------------------------------------------

class TestAgentContentInPrompt:
    def test_business_analyst_guidelines_in_prompt(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary", "Functional Requirements", "User Stories"])
        prompt = BUILDER.build(ctx)
        assert "Do NOT include effort estimates" in prompt

    def test_business_analyst_quality_standards_in_prompt(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary", "Functional Requirements"])
        prompt = BUILDER.build(ctx)
        assert "INVEST principles" in prompt

    def test_idea_researcher_guidelines_in_prompt(self):
        ctx = _make_ctx("idea_researcher", "Idea Researcher", "I research ideas.", [])
        prompt = BUILDER.build(ctx)
        assert "explore and build out the idea" in prompt

    def test_software_architect_guidelines_in_prompt(self):
        ctx = _make_ctx("software_architect", "Software Architect", "I design systems.",
                        ["Architecture Overview"])
        prompt = BUILDER.build(ctx)
        # guidelines.md starts with "**Project-Specific Expert Agents**"
        assert "Project-Specific Expert Agents" in prompt

    def test_senior_software_engineer_guidelines_in_prompt(self):
        ctx = _make_ctx("senior_software_engineer", "Senior Software Engineer", "I code.",
                        ["Changes Made"],
                        makes_code_changes=True, filesystem_write_allowed=True,
                        prompt_variant="implementation")
        prompt = BUILDER.build(ctx)
        assert "Implement the code changes" in prompt

    def test_technical_writer_guidelines_in_prompt(self):
        ctx = _make_ctx("technical_writer", "Technical Writer", "I write docs.",
                        ["Documentation"])
        prompt = BUILDER.build(ctx)
        assert "Documentation Creation Guidelines" in prompt

    def test_dev_environment_setup_guidelines_in_prompt(self):
        ctx = _make_ctx("dev_environment_setup", "Dev Env Setup", "I set up envs.",
                        [], filesystem_write_allowed=True)
        prompt = BUILDER.build(ctx)
        assert "Your Task" in prompt

    def test_work_breakdown_guidelines_in_prompt(self):
        ctx = _make_ctx("work_breakdown_agent", "Work Breakdown", "I break down work.",
                        [])
        prompt = BUILDER.build(ctx)
        assert "Important Guidelines" in prompt


# ---------------------------------------------------------------------------
# Prompt variant — standard vs implementation
# ---------------------------------------------------------------------------

class TestPromptVariant:
    def test_standard_includes_output_format_header(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"])
        prompt = BUILDER.build(ctx)
        assert "## Output Format" in prompt

    def test_implementation_excludes_output_format_header(self):
        ctx = _make_ctx("senior_software_engineer", "Senior Software Engineer", "I code.",
                        ["Changes Made"],
                        makes_code_changes=True, filesystem_write_allowed=True,
                        prompt_variant="implementation")
        prompt = BUILDER.build(ctx)
        assert "## Output Format" not in prompt

    def test_implementation_includes_issue_body_directly(self):
        ctx = _make_ctx("senior_software_engineer", "Senior Software Engineer", "I code.",
                        ["Changes Made"],
                        makes_code_changes=True, filesystem_write_allowed=True,
                        prompt_variant="implementation")
        prompt = BUILDER.build(ctx)
        assert "**Description**:" in prompt
        assert "Users need search" in prompt


# ---------------------------------------------------------------------------
# Sub-issue format block (WorkBreakdownAgent)
# ---------------------------------------------------------------------------

class TestSubIssueFormat:
    def test_sub_issue_block_appended_when_flag_set(self):
        ctx = _make_ctx("work_breakdown_agent", "Work Breakdown", "I break down work.",
                        [], include_sub_issue_format=True)
        ctx.sub_issue_parent_issue_number = "42"
        ctx.sub_issue_discussion_reference_json = '{"id": "D_1"}'
        prompt = BUILDER.build(ctx)
        # sub_issue_format.md contains {parent_issue_number} expanded to "42"
        assert "42" in prompt

    def test_sub_issue_block_absent_when_flag_not_set(self):
        loader = ContentLoader()
        raw_block = loader.agent_sub_issue_format("work_breakdown_agent")
        if not raw_block:
            pytest.skip("sub_issue_format.md is empty")
        # Use a phrase unique to sub_issue_format.md that won't appear in the
        # base _INITIAL_STANDARD template (e.g. not "## Output Format").
        # The JSON schema instruction is distinctive enough.
        unique_phrase = "Output ONLY a"
        assert unique_phrase in raw_block, "sub_issue_format.md changed — update test phrase"

        ctx = _make_ctx("work_breakdown_agent", "Work Breakdown", "I break down work.", [])
        prompt = BUILDER.build(ctx)
        assert unique_phrase not in prompt


# ---------------------------------------------------------------------------
# Revision mode
# ---------------------------------------------------------------------------

class TestRevisionMode:
    def test_embedded_revision_includes_feedback(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"],
                        task_extras={
                            "trigger": "feedback_loop",
                            "feedback": {"formatted_text": "Please add more detail"},
                            "previous_output": "old output text",
                        })
        assert ctx.mode == "revision"
        prompt = BUILDER.build(ctx)
        assert "Please add more detail" in prompt
        assert "old output text" in prompt

    def test_review_cycle_revision_includes_cycle_context(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"],
                        task_extras={
                            "trigger": "review_cycle_revision",
                            "review_cycle": {
                                "iteration": 2,
                                "max_iterations": 3,
                                "reviewer_agent": "code_reviewer",
                                "is_rereviewing": False,
                                "previous_review_feedback": "Fix tests",
                            }
                        })
        prompt = BUILDER.build(ctx)
        assert "Review Cycle" in prompt
        assert "Revision" in prompt

    def test_file_based_revision_references_context_files(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"],
                        task_extras={
                            "trigger": "review_cycle_revision",
                            "review_cycle_context_dir": "/review_cycle_context",
                            "review_cycle": {
                                "iteration": 2,
                                "max_iterations": 3,
                                "reviewer_agent": "code_reviewer",
                                "is_rereviewing": False,
                                "previous_review_feedback": "Fix X",
                            }
                        })
        prompt = BUILDER.build(ctx)
        assert "review_feedback_2.md" in prompt
        assert "maker_output_2.md" in prompt

    def test_feedback_loop_trigger_uses_feedback_context(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.", [],
                        task_extras={
                            "trigger": "feedback_loop",
                            "feedback": {"formatted_text": "tweak this"},
                        })
        assert ctx.mode == "revision"
        prompt = BUILDER.build(ctx)
        assert "Feedback Context" in prompt

    def test_revision_notes_structure_described(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"],
                        task_extras={
                            "trigger": "review_cycle_revision",
                            "review_cycle": {
                                "iteration": 1, "max_iterations": 3,
                                "reviewer_agent": "code_reviewer",
                            },
                            "feedback": {"formatted_text": "Fix X"},
                            "previous_output": "old",
                        })
        prompt = BUILDER.build(ctx)
        assert "Revision Notes" in prompt


# ---------------------------------------------------------------------------
# Reviewer prompts (CodeReviewer, DocumentationEditor)
# ---------------------------------------------------------------------------

class TestReviewerPrompt:
    def _reviewer_ctx(self, agent_name, is_rereviewing=False, post_human=False,
                      with_context_dir=False, with_previous_stage=""):
        rc = ReviewCycleContext(
            iteration=2,
            max_iterations=3,
            maker_agent="senior_software_engineer",
            reviewer_agent=agent_name,
            is_rereviewing=is_rereviewing,
            post_human_feedback=post_human,
            previous_review_feedback="You missed error handling" if is_rereviewing else "",
        )
        ctx = PromptContext(
            agent_name=agent_name,
            agent_display_name="Senior Software Engineer",
            agent_role_description="I review code.",
            output_sections=[],
            project="myapp",
            issue=IssueContext(title="Add search", body="Users need search"),
            review_cycle=rc,
            review_cycle_context_dir="/review_cycle_context" if with_context_dir else None,
            previous_stage=with_previous_stage,
        )
        return ctx

    def test_code_reviewer_initial_includes_review_cycle_initial(self):
        ctx = self._reviewer_ctx("code_reviewer")
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "Initial Review" in prompt

    def test_code_reviewer_rereviewing_includes_re_review_context(self):
        ctx = self._reviewer_ctx("code_reviewer", is_rereviewing=True)
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "Re-Review" in prompt

    def test_code_reviewer_post_human_includes_post_human_context(self):
        ctx = self._reviewer_ctx("code_reviewer", post_human=True)
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "Post-Escalation" in prompt

    def test_code_reviewer_includes_review_task_content(self):
        ctx = self._reviewer_ctx("code_reviewer", with_context_dir=True)
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        loader = ContentLoader()
        review_task_phrase = loader.agent_review_task("code_reviewer").split("\n")[0][:30]
        assert review_task_phrase in prompt

    def test_code_reviewer_initial_format_instructions_used(self):
        ctx = self._reviewer_ctx("code_reviewer")
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        loader = ContentLoader()
        initial_phrase = loader.agent_format_initial("code_reviewer").split("\n")[0][:30]
        assert initial_phrase in prompt

    def test_code_reviewer_rereviewing_format_instructions_used(self):
        ctx = self._reviewer_ctx("code_reviewer", is_rereviewing=True)
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        loader = ContentLoader()
        rereview_phrase = loader.agent_format_rereviewing("code_reviewer").split("\n")[0][:30]
        assert rereview_phrase in prompt

    def test_code_reviewer_context_dir_lists_files(self):
        ctx = self._reviewer_ctx("code_reviewer", with_context_dir=True)
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "current_diff.md" in prompt
        assert "maker_output_2.md" in prompt

    def test_code_reviewer_rereviewing_with_context_dir_lists_prev_feedback_file(self):
        ctx = self._reviewer_ctx("code_reviewer", is_rereviewing=True, with_context_dir=True)
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "review_feedback_1.md" in prompt

    def test_documentation_editor_embeds_previous_stage_as_docs_section(self):
        ctx = self._reviewer_ctx("documentation_editor",
                                 with_previous_stage="# My Doc\n\nSome content")
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Documentation Editor",
                                               review_domain="documentation")
        assert "Documentation to Review" in prompt
        assert "My Doc" in prompt

    def test_reviewer_prompt_does_not_create_files(self):
        ctx = self._reviewer_ctx("code_reviewer", with_context_dir=True)
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "DO NOT create any files" in prompt

    def test_filter_instructions_injected(self):
        ctx = self._reviewer_ctx("code_reviewer")
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code",
                                               filter_instructions="## Filter: skip X")
        assert "## Filter: skip X" in prompt

    def test_previous_review_feedback_appears_in_rereviewing(self):
        ctx = self._reviewer_ctx("code_reviewer", is_rereviewing=True)
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "You missed error handling" in prompt


# ---------------------------------------------------------------------------
# Verifier prompt (DevEnvironmentVerifier)
# ---------------------------------------------------------------------------

class TestVerifierPrompt:
    def _verifier_ctx(self, is_rereviewing=False, prior_feedback=""):
        rc = ReviewCycleContext(
            iteration=1,
            max_iterations=3,
            is_rereviewing=is_rereviewing,
            previous_review_feedback=prior_feedback,
        )
        return PromptContext(
            agent_name="dev_environment_verifier",
            agent_display_name="Dev Environment Verifier",
            agent_role_description="",
            output_sections=[],
            project="myproject",
            project_name="myproject",
            issue=IssueContext(title="Set up env", body="Need Docker image"),
            previous_stage="Setup agent output here",
            review_cycle=rc,
        )

    def test_verifier_expands_project_name_placeholder(self):
        ctx = self._verifier_ctx()
        prompt = BUILDER.build_verifier_prompt(ctx)
        # review_task.md contains {project_name} placeholders — all should be replaced
        assert "{project_name}" not in prompt
        assert "myproject" in prompt

    def test_verifier_includes_previous_stage(self):
        ctx = self._verifier_ctx()
        prompt = BUILDER.build_verifier_prompt(ctx)
        assert "Setup agent output here" in prompt

    def test_verifier_initial_includes_initial_context(self):
        ctx = self._verifier_ctx()
        prompt = BUILDER.build_verifier_prompt(ctx)
        assert "Initial Verification" in prompt

    def test_verifier_rereviewing_includes_re_verification_context(self):
        ctx = self._verifier_ctx(is_rereviewing=True, prior_feedback="Image was broken")
        prompt = BUILDER.build_verifier_prompt(ctx)
        assert "Re-Verification" in prompt

    def test_verifier_rereviewing_includes_prior_feedback(self):
        ctx = self._verifier_ctx(is_rereviewing=True, prior_feedback="Image was broken")
        prompt = BUILDER.build_verifier_prompt(ctx)
        assert "Image was broken" in prompt

    def test_verifier_includes_issue_details(self):
        ctx = self._verifier_ctx()
        prompt = BUILDER.build_verifier_prompt(ctx)
        assert "Set up env" in prompt


# ---------------------------------------------------------------------------
# build_from_template (PRCodeReviewer, RequirementsVerifier)
# ---------------------------------------------------------------------------

class TestBuildFromTemplate:
    def _pr_ctx(self, agent_name, pr_url="https://github.com/org/repo/pull/42",
                check_name="", check_content=""):
        return PromptContext(
            agent_name=agent_name,
            agent_display_name="PR Reviewer",
            agent_role_description="I review PRs.",
            output_sections=[],
            pr_url=pr_url,
            check_name=check_name,
            check_content=check_content,
        )

    def test_pr_code_reviewer_expands_pr_url(self):
        ctx = self._pr_ctx("pr_code_reviewer")
        prompt = BUILDER.build_from_template(ctx)
        assert "https://github.com/org/repo/pull/42" in prompt

    def test_requirements_verifier_expands_all_placeholders(self):
        ctx = self._pr_ctx("requirements_verifier",
                           check_name="Acceptance Criteria",
                           check_content="The system must do X")
        prompt = BUILDER.build_from_template(ctx)
        assert "https://github.com/org/repo/pull/42" in prompt
        assert "Acceptance Criteria" in prompt
        assert "The system must do X" in prompt

    def test_check_content_truncated_at_15000_chars(self):
        long_content = "x" * 20000
        ctx = self._pr_ctx("requirements_verifier",
                           check_name="spec",
                           check_content=long_content)
        prompt = BUILDER.build_from_template(ctx)
        assert "truncated" in prompt
        # The content portion should not exceed 15000 + overhead
        assert len(prompt) < 20000

    def test_check_content_not_truncated_when_under_limit(self):
        content = "requirement: do X"
        ctx = self._pr_ctx("requirements_verifier", check_name="spec", check_content=content)
        prompt = BUILDER.build_from_template(ctx)
        assert "truncated" not in prompt
        assert content in prompt

    def test_no_unresolved_placeholders_in_pr_code_reviewer(self):
        ctx = self._pr_ctx("pr_code_reviewer")
        prompt = BUILDER.build_from_template(ctx)
        import re
        unresolved = re.findall(r"\{[a-z_]+\}", prompt)
        assert not unresolved, f"Unresolved placeholders: {unresolved}"

    def test_no_unresolved_placeholders_in_requirements_verifier(self):
        ctx = self._pr_ctx("requirements_verifier", check_name="spec", check_content="do X")
        prompt = BUILDER.build_from_template(ctx)
        import re
        unresolved = re.findall(r"\{[a-z_]+\}", prompt)
        assert not unresolved, f"Unresolved placeholders: {unresolved}"


# ---------------------------------------------------------------------------
# Previous stage section
# ---------------------------------------------------------------------------

class TestPreviousStageSSection:
    def test_previous_stage_embedded_in_initial_prompt(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"],
                        task_extras={"previous_stage_output": "Prior analysis here"})
        prompt = BUILDER.build(ctx)
        assert "Previous Stage Output" in prompt
        assert "Prior analysis here" in prompt

    def test_no_previous_stage_section_when_absent(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"])
        prompt = BUILDER.build(ctx)
        assert "Previous Stage Output" not in prompt


# ---------------------------------------------------------------------------
# Thread history formatting
# ---------------------------------------------------------------------------

class TestThreadHistoryFormatting:
    def test_user_message_prefixed_with_at(self):
        history = [{"role": "user", "author": "alice", "body": "What about caching?"}]
        result = PromptBuilder._format_thread_history(history)
        assert "**@alice**" in result
        assert "What about caching?" in result

    def test_agent_message_prefixed_with_you(self):
        history = [{"role": "agent", "author": "bot", "body": "Here is the analysis."}]
        result = PromptBuilder._format_thread_history(history)
        assert "**You**" in result
        assert "Here is the analysis." in result

    def test_body_as_dict_extracts_formatted_text(self):
        history = [{"role": "user", "author": "bob",
                    "body": {"formatted_text": "Formatted question", "text": "raw"}}]
        result = PromptBuilder._format_thread_history(history)
        assert "Formatted question" in result

    def test_empty_history_returns_empty_string(self):
        assert PromptBuilder._format_thread_history([]) == ""


# ---------------------------------------------------------------------------
# Fix 1: SeniorSoftwareEngineerAgent previous-stage uses "Previous Work and
#         Feedback" with QA/testing return language, not generic text.
# ---------------------------------------------------------------------------

class TestPreviousWorkSection:
    def test_implementation_previous_stage_uses_strong_language(self):
        ctx = _make_ctx("senior_software_engineer", "Senior Software Engineer", "I code.",
                        ["Changes Made"],
                        task_extras={"previous_stage_output": "QA found three failures."},
                        makes_code_changes=True, filesystem_write_allowed=True,
                        prompt_variant="implementation")
        prompt = BUILDER.build(ctx)
        assert "Previous Work and Feedback" in prompt
        assert "QA found three failures." in prompt
        # Ensure the explicit instruction to address feedback is present
        assert "address every issue" in prompt

    def test_implementation_generic_previous_stage_section_not_used(self):
        # The weak "Build upon this previous analysis" phrasing must NOT appear
        # in the implementation variant — it's only for analysis agents.
        ctx = _make_ctx("senior_software_engineer", "Senior Software Engineer", "I code.",
                        ["Changes Made"],
                        task_extras={"previous_stage_output": "Prior output here."},
                        makes_code_changes=True, filesystem_write_allowed=True,
                        prompt_variant="implementation")
        prompt = BUILDER.build(ctx)
        assert "Build upon this previous analysis" not in prompt

    def test_standard_previous_stage_still_uses_generic_section(self):
        ctx = _make_ctx("business_analyst", "Business Analyst", "I analyse.",
                        ["Executive Summary"],
                        task_extras={"previous_stage_output": "Prior analysis here."})
        prompt = BUILDER.build(ctx)
        assert "Previous Stage Output" in prompt
        assert "Build upon this previous analysis" in prompt


# ---------------------------------------------------------------------------
# Fix 2: DocumentationEditorAgent re-review uses documentation-specific
#         common issues, not the code-focused shared template.
# ---------------------------------------------------------------------------

class TestDocumentationEditorRereviewing:
    def _doc_editor_rereviewing_ctx(self):
        rc = ReviewCycleContext(
            iteration=2,
            max_iterations=3,
            maker_agent="technical_writer",
            reviewer_agent="documentation_editor",
            is_rereviewing=True,
            previous_review_feedback="Section 3 has placeholder content.",
        )
        return PromptContext(
            agent_name="documentation_editor",
            agent_display_name="Senior Documentation Editor",
            agent_role_description="I review documentation.",
            output_sections=[],
            project="myapp",
            issue=IssueContext(title="Write API docs", body="Document the API"),
            review_cycle=rc,
            previous_stage="# API Docs\n\nContent here.",
        )

    def test_documentation_specific_common_issues_present(self):
        ctx = self._doc_editor_rereviewing_ctx()
        prompt = BUILDER.build_reviewer_prompt(ctx,
                                               reviewer_title="Senior Documentation Editor",
                                               review_domain="documentation")
        assert "Placeholder content" in prompt
        assert "Marketing fluff" in prompt
        assert "Broken links" in prompt

    def test_code_specific_common_issues_absent(self):
        ctx = self._doc_editor_rereviewing_ctx()
        prompt = BUILDER.build_reviewer_prompt(ctx,
                                               reviewer_title="Senior Documentation Editor",
                                               review_domain="documentation")
        # "Phase X" naming is a code-review issue that must NOT appear in doc editor re-review
        assert 'names including "Phase X"' not in prompt

    def test_code_reviewer_rereviewing_uses_shared_template(self):
        rc = ReviewCycleContext(
            iteration=2, max_iterations=3,
            maker_agent="senior_software_engineer",
            reviewer_agent="code_reviewer",
            is_rereviewing=True,
            previous_review_feedback="Fix the error handling.",
        )
        ctx = PromptContext(
            agent_name="code_reviewer",
            agent_display_name="Senior Software Engineer",
            agent_role_description="I review code.",
            output_sections=[],
            project="myapp",
            issue=IssueContext(title="Add search", body="desc"),
            review_cycle=rc,
            review_cycle_context_dir="/review_cycle_context",
        )
        prompt = BUILDER.build_reviewer_prompt(ctx,
                                               reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        # Code reviewer should get code-specific common issues
        assert 'Phase X' in prompt or 'Markdown files' in prompt


# ---------------------------------------------------------------------------
# Fix 3: change_manifest is an explicit PromptContext field, not a dynamic
#         attribute injected after construction.
# ---------------------------------------------------------------------------

class TestChangeManifestField:
    def test_change_manifest_field_used_in_context_section(self):
        rc = ReviewCycleContext(
            iteration=1, max_iterations=3,
            maker_agent="senior_software_engineer",
            reviewer_agent="code_reviewer",
            is_rereviewing=False,
        )
        ctx = PromptContext(
            agent_name="code_reviewer",
            agent_display_name="Senior Software Engineer",
            agent_role_description="",
            output_sections=[],
            project="myapp",
            issue=IssueContext(title="Add feature", body="desc"),
            review_cycle=rc,
            # No review_cycle_context_dir — falls back to embedded change_manifest
            change_manifest="Changed: src/app.py, tests/test_app.py",
        )
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "Code Changes" in prompt
        assert "src/app.py" in prompt

    def test_change_manifest_not_set_produces_no_code_changes_section(self):
        rc = ReviewCycleContext(iteration=1, max_iterations=3,
                                maker_agent="senior_software_engineer",
                                reviewer_agent="code_reviewer")
        ctx = PromptContext(
            agent_name="code_reviewer",
            agent_display_name="Senior Software Engineer",
            agent_role_description="",
            output_sections=[],
            project="myapp",
            issue=IssueContext(title="Add feature", body="desc"),
            review_cycle=rc,
        )
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain="code")
        assert "Code Changes" not in prompt

    def test_change_manifest_default_is_empty_string(self):
        ctx = PromptContext(
            agent_name="code_reviewer",
            agent_display_name="SR",
            agent_role_description="",
            output_sections=[],
        )
        assert ctx.change_manifest == ""


# ---------------------------------------------------------------------------
# Fix 5: reviewer_initial.md uses {review_domain} so "code" vs "documentation"
#         is preserved in the initial iteration context block.
# ---------------------------------------------------------------------------

class TestReviewerInitialDomain:
    def _initial_reviewer_ctx(self, agent_name, review_domain):
        rc = ReviewCycleContext(
            iteration=1, max_iterations=3,
            maker_agent="technical_writer" if review_domain == "documentation" else "senior_software_engineer",
            reviewer_agent=agent_name,
            is_rereviewing=False,
        )
        return PromptContext(
            agent_name=agent_name,
            agent_display_name="Reviewer",
            agent_role_description="",
            output_sections=[],
            project="myapp",
            issue=IssueContext(title="Task", body="desc"),
            review_cycle=rc,
        ), review_domain

    def test_code_reviewer_initial_mentions_code(self):
        ctx, domain = self._initial_reviewer_ctx("code_reviewer", "code")
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Software Engineer",
                                               review_domain=domain)
        assert "code" in prompt.lower()

    def test_documentation_editor_initial_mentions_documentation(self):
        ctx, domain = self._initial_reviewer_ctx("documentation_editor", "documentation")
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Documentation Editor",
                                               review_domain=domain)
        # "documentation" appears in the iteration context block
        assert "documentation" in prompt.lower()

    def test_initial_context_does_not_say_implemented_the_code_for_docs(self):
        ctx, domain = self._initial_reviewer_ctx("documentation_editor", "documentation")
        prompt = BUILDER.build_reviewer_prompt(ctx, reviewer_title="Senior Documentation Editor",
                                               review_domain=domain)
        # Should NOT say "implemented the code" for documentation domain
        assert "implemented the code" not in prompt
