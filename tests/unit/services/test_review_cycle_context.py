"""Unit tests for ReviewCycleContextWriter"""

import os
import tempfile
import pytest
from unittest.mock import patch

import services.review_cycle_context as rcc_module
from services.review_cycle_context import ReviewCycleContextWriter


@pytest.fixture
def tmp_base(tmp_path):
    """Patch the context base directory to use a temp dir."""
    with patch.object(rcc_module, '_CONTEXT_BASE', str(tmp_path)):
        yield tmp_path


class TestReviewCycleContextWriter:

    def test_setup_creates_directory(self, tmp_base):
        w = ReviewCycleContextWriter.setup(470, 'd3024ce7-abcd')
        assert w.exists()
        assert os.path.isdir(w.context_dir)
        assert '470_d3024ce7' in w.context_dir

    def test_setup_no_pipeline_run_id(self, tmp_base):
        w = ReviewCycleContextWriter.setup(99, '')
        assert w.exists()
        assert '99' in w.context_dir

    def test_write_initial_request(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_initial_request('My Feature', 'Do the thing')
        path = os.path.join(w.context_dir, 'initial_request.md')
        assert os.path.exists(path)
        content = open(path).read()
        assert 'My Feature' in content
        assert 'Do the thing' in content

    def test_write_maker_output(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_maker_output('First implementation', 1)
        w.write_maker_output('Second implementation', 2)
        assert os.path.exists(os.path.join(w.context_dir, 'maker_output_1.md'))
        assert os.path.exists(os.path.join(w.context_dir, 'maker_output_2.md'))

    def test_write_review_feedback(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_review_feedback('Fix this bug', 1)
        assert os.path.exists(os.path.join(w.context_dir, 'review_feedback_1.md'))
        content = open(os.path.join(w.context_dir, 'review_feedback_1.md')).read()
        assert 'Fix this bug' in content

    def test_write_current_diff_overwrites(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_current_diff('first diff')
        w.write_current_diff('second diff')
        content = open(os.path.join(w.context_dir, 'current_diff.md')).read()
        assert content == 'second diff'

    def test_list_files_sorted(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_initial_request('Title', 'Body')
        w.write_maker_output('output', 1)
        w.write_review_feedback('feedback', 1)
        w.write_current_diff('diff')
        files = w.list_files()
        assert files == sorted(files)
        assert 'initial_request.md' in files
        assert 'maker_output_1.md' in files
        assert 'review_feedback_1.md' in files
        assert 'current_diff.md' in files

    def test_from_existing_reattaches(self, tmp_base):
        w = ReviewCycleContextWriter.setup(5, 'xyz99')
        w.write_initial_request('T', 'B')
        w2 = ReviewCycleContextWriter.from_existing(w.context_dir)
        assert w2.exists()
        assert w2.list_files() == w.list_files()

    def test_repopulate_from_state(self, tmp_base):
        w = ReviewCycleContextWriter.from_existing(str(tmp_base / '5_xyz99'))
        assert not w.exists()

        maker_outputs = [
            {'iteration': 0, 'output': 'initial maker output'},
            {'iteration': 1, 'output': 'revised maker output'},
        ]
        review_outputs = [
            {'iteration': 1, 'output': 'reviewer feedback'},
        ]
        issue = {'title': 'My Issue', 'body': 'Do the thing'}

        w.repopulate_from_state(issue, maker_outputs, review_outputs)
        assert w.exists()
        files = w.list_files()
        assert 'initial_request.md' in files
        assert 'maker_output_1.md' in files   # iteration 0 → file 1
        assert 'maker_output_2.md' in files   # iteration 1 → file 2
        assert 'review_feedback_1.md' in files

    def test_repopulate_idempotent(self, tmp_base):
        """Repopulate should not overwrite existing files."""
        w = ReviewCycleContextWriter.setup(1, 'idem')
        w.write_initial_request('Original', 'Original body')

        maker_outputs = [{'iteration': 0, 'output': 'new initial'}]
        w.repopulate_from_state({'title': 'New Title', 'body': 'New body'}, maker_outputs, [])

        content = open(os.path.join(w.context_dir, 'initial_request.md')).read()
        assert 'Original' in content  # Not overwritten

    def test_maker_prompt_section_contains_file_refs(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_initial_request('T', 'B')
        w.write_maker_output('output', 1)
        w.write_review_feedback('feedback', 1)
        section = w.maker_prompt_section(1)
        assert 'review_feedback_1.md' in section
        assert '/workspace/review_cycle_context/' in section
        assert 'initial_request.md' in section

    def test_reviewer_prompt_section_contains_file_refs(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_initial_request('T', 'B')
        w.write_maker_output('output', 1)
        w.write_current_diff('diff')
        section = w.reviewer_prompt_section(1)
        assert 'current_diff.md' in section
        assert 'initial_request.md' in section
        assert '/workspace/review_cycle_context/' in section

    def test_reviewer_prompt_section_rerereview_includes_prev_feedback(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_review_feedback('prev', 1)
        section = w.reviewer_prompt_section(2)
        assert 'review_feedback_1.md' in section

    def test_cleanup_removes_directory(self, tmp_base):
        w = ReviewCycleContextWriter.setup(1, 'abc12345')
        w.write_initial_request('T', 'B')
        assert w.exists()
        w.cleanup()
        assert not w.exists()

    def test_cleanup_missing_dir_is_safe(self, tmp_base):
        w = ReviewCycleContextWriter.from_existing(str(tmp_base / 'nonexistent'))
        w.cleanup()  # Should not raise

    def test_empty_dir_returns_empty_list(self, tmp_base):
        w = ReviewCycleContextWriter.from_existing(str(tmp_base / 'nonexistent'))
        assert w.list_files() == []
