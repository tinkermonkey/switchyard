"""Unit tests for PipelineContextWriter (including review-cycle methods)"""

import os
import tempfile
import pytest
from unittest.mock import patch

import services.pipeline_context_writer as pcw_module
from services.pipeline_context_writer import PipelineContextWriter


@pytest.fixture
def tmp_base(tmp_path):
    """Patch the context base directory to use a temp dir."""
    with patch.object(pcw_module, '_PIPELINE_CONTEXT_BASE', str(tmp_path)):
        yield tmp_path


class TestPipelineContextWriterSetup:

    def test_setup_creates_directory(self, tmp_base):
        w = PipelineContextWriter.setup(470, 'd3024ce7-abcd')
        assert w.exists()
        assert os.path.isdir(w.context_dir)
        assert '470_d3024ce7' in w.context_dir

    def test_setup_no_pipeline_run_id(self, tmp_base):
        w = PipelineContextWriter.setup(99, '')
        assert w.exists()
        assert '99' in w.context_dir

    def test_write_initial_request(self, tmp_base):
        w = PipelineContextWriter.setup(1, 'abc12345')
        w.write_initial_request('My Feature', 'Do the thing')
        path = os.path.join(w.context_dir, 'initial_request.md')
        assert os.path.exists(path)
        content = open(path).read()
        assert 'My Feature' in content
        assert 'Do the thing' in content

    def test_list_files_sorted(self, tmp_base):
        w = PipelineContextWriter.setup(1, 'abc12345')
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
        w = PipelineContextWriter.setup(5, 'xyz99')
        w.write_initial_request('T', 'B')
        w2 = PipelineContextWriter.from_existing(w.context_dir)
        assert w2.exists()
        assert w2.list_files() == w.list_files()

    def test_cleanup_removes_directory(self, tmp_base):
        w = PipelineContextWriter.setup(1, 'abc12345')
        w.write_initial_request('T', 'B')
        assert w.exists()
        w.cleanup()
        assert not w.exists()

    def test_cleanup_missing_dir_is_safe(self, tmp_base):
        w = PipelineContextWriter.from_existing(str(tmp_base / 'nonexistent'))
        w.cleanup()  # Should not raise

    def test_empty_dir_returns_empty_list(self, tmp_base):
        w = PipelineContextWriter.from_existing(str(tmp_base / 'nonexistent'))
        assert w.list_files() == []


class TestReviewCycleMethods:

    def test_write_maker_output(self, tmp_base):
        w = PipelineContextWriter.setup(1, 'abc12345')
        w.write_maker_output('First implementation', 1)
        w.write_maker_output('Second implementation', 2)
        assert os.path.exists(os.path.join(w.context_dir, 'maker_output_1.md'))
        assert os.path.exists(os.path.join(w.context_dir, 'maker_output_2.md'))

    def test_write_review_feedback(self, tmp_base):
        w = PipelineContextWriter.setup(1, 'abc12345')
        w.write_review_feedback('Fix this bug', 1)
        assert os.path.exists(os.path.join(w.context_dir, 'review_feedback_1.md'))
        content = open(os.path.join(w.context_dir, 'review_feedback_1.md')).read()
        assert 'Fix this bug' in content

    def test_write_current_diff_overwrites(self, tmp_base):
        w = PipelineContextWriter.setup(1, 'abc12345')
        w.write_current_diff('first diff')
        w.write_current_diff('second diff')
        content = open(os.path.join(w.context_dir, 'current_diff.md')).read()
        assert content == 'second diff'

    def test_repopulate_review_cycle_from_state(self, tmp_base):
        w = PipelineContextWriter.from_existing(str(tmp_base / '5_xyz99'))
        assert not w.exists()

        maker_outputs = [
            {'iteration': 0, 'output': 'initial maker output'},
            {'iteration': 1, 'output': 'revised maker output'},
        ]
        review_outputs = [
            {'iteration': 1, 'output': 'reviewer feedback'},
        ]
        issue = {'title': 'My Issue', 'body': 'Do the thing'}

        w.repopulate_review_cycle_from_state(issue, maker_outputs, review_outputs)
        assert w.exists()
        files = w.list_files()
        assert 'initial_request.md' in files
        assert 'maker_output_1.md' in files   # iteration 0 → file 1
        assert 'maker_output_2.md' in files   # iteration 1 → file 2
        assert 'review_feedback_1.md' in files

    def test_repopulate_review_cycle_idempotent(self, tmp_base):
        """Repopulate should not overwrite existing files."""
        w = PipelineContextWriter.setup(1, 'idem')
        w.write_initial_request('Original', 'Original body')

        maker_outputs = [{'iteration': 0, 'output': 'new initial'}]
        w.repopulate_review_cycle_from_state(
            {'title': 'New Title', 'body': 'New body'}, maker_outputs, []
        )

        content = open(os.path.join(w.context_dir, 'initial_request.md')).read()
        assert 'Original' in content  # Not overwritten
