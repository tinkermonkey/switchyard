"""
Unit tests for elasticsearch_utils.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from services.medic.shared.elasticsearch_utils import (
    setup_index_template,
    async_index,
    update_by_query,
    search_signatures,
    get_signature_by_id,
    delete_by_query
)


@pytest.fixture
def mock_es_client():
    """Create a mock Elasticsearch client."""
    client = Mock()
    client.ilm = Mock()
    client.indices = Mock()
    return client


class TestSetupIndexTemplate:
    """Tests for setup_index_template function."""

    def test_setup_template_without_ilm(self, mock_es_client):
        """Test setting up index template without ILM policy."""
        result = setup_index_template(
            mock_es_client,
            "test-template",
            ["test-index-*"],
            {"mappings": {"properties": {}}}
        )

        assert result is True
        mock_es_client.indices.put_index_template.assert_called_once()

    def test_setup_template_with_ilm(self, mock_es_client):
        """Test setting up index template with ILM policy."""
        mock_es_client.ilm.get_lifecycle.return_value = None

        result = setup_index_template(
            mock_es_client,
            "test-template",
            ["test-index-*"],
            {"mappings": {"properties": {}}},
            ilm_policy_name="test-ilm",
            ilm_policy_body={"policy": {}}
        )

        assert result is True
        mock_es_client.ilm.put_lifecycle.assert_called_once_with(
            name="test-ilm",
            body={"policy": {}}
        )
        mock_es_client.indices.put_index_template.assert_called_once()

    def test_setup_template_ilm_already_exists(self, mock_es_client):
        """Test that existing ILM policy is not recreated."""
        mock_es_client.ilm.get_lifecycle.return_value = {"existing": "policy"}

        result = setup_index_template(
            mock_es_client,
            "test-template",
            ["test-index-*"],
            {"mappings": {"properties": {}}},
            ilm_policy_name="test-ilm",
            ilm_policy_body={"policy": {}}
        )

        assert result is True
        mock_es_client.ilm.put_lifecycle.assert_not_called()

    def test_setup_template_exception_handling(self, mock_es_client):
        """Test that exceptions are handled gracefully."""
        mock_es_client.indices.put_index_template.side_effect = Exception("ES error")

        result = setup_index_template(
            mock_es_client,
            "test-template",
            ["test-index-*"],
            {"mappings": {"properties": {}}}
        )

        assert result is False


class TestAsyncIndex:
    """Tests for async_index function."""

    @pytest.mark.asyncio
    async def test_async_index_success(self, mock_es_client):
        """Test successful document indexing."""
        document = {"field": "value"}

        result = await async_index(mock_es_client, "test-index", "doc-id", document)

        assert result is True
        mock_es_client.index.assert_called_once_with(
            index="test-index",
            id="doc-id",
            document=document
        )

    @pytest.mark.asyncio
    async def test_async_index_exception_handling(self, mock_es_client):
        """Test that exceptions are handled gracefully."""
        mock_es_client.index.side_effect = Exception("ES error")

        result = await async_index(mock_es_client, "test-index", "doc-id", {})

        assert result is False


class TestUpdateByQuery:
    """Tests for update_by_query function."""

    def test_update_by_query_success(self, mock_es_client):
        """Test successful update by query."""
        mock_es_client.update_by_query.return_value = {"updated": 5}

        script = {"source": "ctx._source.field = params.value", "params": {"value": "new"}}
        query = {"term": {"id": "123"}}

        result = update_by_query(mock_es_client, "test-index-*", script, query)

        assert result == {"updated": 5}
        mock_es_client.update_by_query.assert_called_once()

    def test_update_by_query_exception_handling(self, mock_es_client):
        """Test that exceptions are handled gracefully."""
        mock_es_client.update_by_query.side_effect = Exception("ES error")

        result = update_by_query(mock_es_client, "test-index-*", {}, {})

        assert result is None


class TestSearchSignatures:
    """Tests for search_signatures function."""

    def test_search_signatures_success(self, mock_es_client):
        """Test successful signature search."""
        mock_es_client.search.return_value = {
            "hits": {"total": {"value": 1}, "hits": [{"_source": {"id": "123"}}]}
        }

        query = {"match_all": {}}
        result = search_signatures(mock_es_client, "test-index-*", query, size=10)

        assert result is not None
        assert result["hits"]["total"]["value"] == 1
        mock_es_client.search.assert_called_once()

    def test_search_signatures_with_source_fields(self, mock_es_client):
        """Test search with specific source fields."""
        mock_es_client.search.return_value = {"hits": {"hits": []}}

        query = {"match_all": {}}
        source_fields = ["field1", "field2"]

        result = search_signatures(
            mock_es_client,
            "test-index-*",
            query,
            source_fields=source_fields
        )

        # Verify _source was included in search body
        call_args = mock_es_client.search.call_args
        assert call_args[1]["body"]["_source"] == source_fields

    def test_search_signatures_exception_handling(self, mock_es_client):
        """Test that exceptions are handled gracefully."""
        mock_es_client.search.side_effect = Exception("ES error")

        result = search_signatures(mock_es_client, "test-index-*", {})

        assert result is None


class TestGetSignatureById:
    """Tests for get_signature_by_id function."""

    def test_get_signature_by_id_found(self, mock_es_client):
        """Test getting signature when it exists."""
        mock_es_client.search.return_value = {
            "hits": {
                "hits": [{"_source": {"fingerprint_id": "fp123", "data": "value"}}]
            }
        }

        result = get_signature_by_id(mock_es_client, "test-index-*", "fp123")

        assert result is not None
        assert result["fingerprint_id"] == "fp123"

    def test_get_signature_by_id_not_found(self, mock_es_client):
        """Test getting signature when it doesn't exist."""
        mock_es_client.search.return_value = {"hits": {"hits": []}}

        result = get_signature_by_id(mock_es_client, "test-index-*", "fp123")

        assert result is None

    def test_get_signature_by_id_exception_handling(self, mock_es_client):
        """Test that exceptions are handled gracefully."""
        mock_es_client.search.side_effect = Exception("ES error")

        result = get_signature_by_id(mock_es_client, "test-index-*", "fp123")

        assert result is None


class TestDeleteByQuery:
    """Tests for delete_by_query function."""

    def test_delete_by_query_success(self, mock_es_client):
        """Test successful delete by query."""
        mock_es_client.delete_by_query.return_value = {"deleted": 10}

        query = {"term": {"status": "old"}}
        result = delete_by_query(mock_es_client, "test-index-*", query, refresh=True)

        assert result == {"deleted": 10}
        mock_es_client.delete_by_query.assert_called_once_with(
            index="test-index-*",
            body={"query": query},
            refresh=True
        )

    def test_delete_by_query_exception_handling(self, mock_es_client):
        """Test that exceptions are handled gracefully."""
        mock_es_client.delete_by_query.side_effect = Exception("ES error")

        result = delete_by_query(mock_es_client, "test-index-*", {})

        assert result is None
