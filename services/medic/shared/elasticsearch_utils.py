"""
Elasticsearch utilities for Medic failure signature storage.

Provides common Elasticsearch operations used by both Docker and Claude systems.
"""

import logging
from typing import Dict, Any, List, Optional
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)


def setup_index_template(
    es_client: Elasticsearch,
    template_name: str,
    index_patterns: List[str],
    mapping: Dict[str, Any],
    ilm_policy_name: Optional[str] = None,
    ilm_policy_body: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Setup Elasticsearch index template with optional ILM policy.

    Args:
        es_client: Elasticsearch client
        template_name: Name of the index template
        index_patterns: List of index patterns (e.g., ["medic-docker-failures-*"])
        mapping: Index mapping configuration
        ilm_policy_name: Optional ILM policy name
        ilm_policy_body: Optional ILM policy configuration

    Returns:
        True if successful, False otherwise
    """
    try:
        # Create ILM policy if provided
        if ilm_policy_name and ilm_policy_body:
            existing_policy = es_client.ilm.get_lifecycle(name=ilm_policy_name, ignore=[404])
            if not existing_policy:
                es_client.ilm.put_lifecycle(name=ilm_policy_name, body=ilm_policy_body)
                logger.info(f"Created ILM policy: {ilm_policy_name}")

        # Create index template
        template_body = {
            "index_patterns": index_patterns,
            "template": mapping,
        }

        es_client.indices.put_index_template(name=template_name, body=template_body)
        logger.info(f"Created index template: {template_name} for patterns: {index_patterns}")
        return True

    except Exception as e:
        logger.error(f"Failed to setup index template {template_name}: {e}")
        return False


async def async_index(
    es_client: Elasticsearch,
    index: str,
    id: str,
    document: Dict[str, Any]
) -> bool:
    """
    Async wrapper for Elasticsearch index operation.

    Args:
        es_client: Elasticsearch client
        index: Index name
        id: Document ID
        document: Document to index

    Returns:
        True if successful, False otherwise
    """
    try:
        # Note: Using sync ES client but wrapped for consistency
        # Future: Could use async Elasticsearch client
        es_client.index(index=index, id=id, document=document)
        return True
    except Exception as e:
        logger.error(f"Failed to index document {id} in {index}: {e}")
        return False


def update_by_query(
    es_client: Elasticsearch,
    index_pattern: str,
    script: Dict[str, Any],
    query: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Execute update_by_query operation.

    Args:
        es_client: Elasticsearch client
        index_pattern: Index pattern to update
        script: Update script with source and params
        query: Query to match documents

    Returns:
        Update result dict or None on failure
    """
    try:
        result = es_client.update_by_query(
            index=index_pattern,
            body={
                "script": script,
                "query": query,
            },
        )
        return result
    except Exception as e:
        logger.error(f"Failed to update_by_query on {index_pattern}: {e}")
        return None


def search_signatures(
    es_client: Elasticsearch,
    index_pattern: str,
    query: Dict[str, Any],
    size: int = 100,
    source_fields: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """
    Search for failure signatures.

    Args:
        es_client: Elasticsearch client
        index_pattern: Index pattern to search
        query: Elasticsearch query
        size: Maximum number of results
        source_fields: Optional list of fields to return

    Returns:
        Search result dict or None on failure
    """
    try:
        search_body = {
            "query": query,
            "size": size,
        }

        if source_fields:
            search_body["_source"] = source_fields

        result = es_client.search(index=index_pattern, body=search_body)
        return result
    except Exception as e:
        logger.error(f"Failed to search {index_pattern}: {e}")
        return None


def get_signature_by_id(
    es_client: Elasticsearch,
    index_pattern: str,
    fingerprint_id: str
) -> Optional[Dict[str, Any]]:
    """
    Get a single signature by fingerprint ID.

    Args:
        es_client: Elasticsearch client
        index_pattern: Index pattern to search
        fingerprint_id: Fingerprint ID to find

    Returns:
        Signature document or None if not found
    """
    query = {"term": {"fingerprint_id": fingerprint_id}}
    result = search_signatures(es_client, index_pattern, query, size=1)

    if result and result.get("hits", {}).get("hits"):
        return result["hits"]["hits"][0]["_source"]

    return None


def delete_by_query(
    es_client: Elasticsearch,
    index_pattern: str,
    query: Dict[str, Any],
    refresh: bool = True
) -> Optional[Dict[str, Any]]:
    """
    Delete documents matching query.

    Args:
        es_client: Elasticsearch client
        index_pattern: Index pattern to delete from
        query: Query to match documents to delete
        refresh: Whether to refresh indices after delete

    Returns:
        Delete result dict or None on failure
    """
    try:
        result = es_client.delete_by_query(
            index=index_pattern,
            body={"query": query},
            refresh=refresh
        )
        return result
    except Exception as e:
        logger.error(f"Failed to delete_by_query on {index_pattern}: {e}")
        return None
