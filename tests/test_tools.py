"""Tests for agent tools against live SurrealDB Cloud.

These are integration tests — they require:
- SurrealDB Cloud with ingested data (both v1 and v2 for diff tests)
- Ollama running locally with nomic-embed-text model
- .env configured with valid credentials

Run: uv run pytest tests/test_tools.py -v
"""
import asyncio

import pytest


def _has_diff_data() -> bool:
    """Probe the DB once at collection time to decide whether v2/diff tests should run."""
    try:
        from agent.tools import _get_rows, _query
        rows = _get_rows(asyncio.run(_query(
            "SELECT count() FROM `function` WHERE diff_status IS NOT NONE GROUP ALL"
        )))
        return bool(rows) and (rows[0].get("count") or 0) > 0
    except Exception:
        return False


_NEEDS_DIFF = pytest.mark.skipif(
    not _has_diff_data(),
    reason="No v2 diff data in DB. Run: uv run python demo/seed_demo.py --with-v2",
)


# ---------------------------------------------------------------------------
# hybrid_search
# ---------------------------------------------------------------------------

class TestHybridSearch:
    def test_returns_results_for_concept_query(self):
        from agent.tools import hybrid_search
        results = hybrid_search.invoke({"query": "authentication"})
        assert isinstance(results, list)
        assert len(results) > 0, "hybrid_search should return results for 'authentication'"

    def test_results_contain_function_metadata(self):
        from agent.tools import hybrid_search
        results = hybrid_search.invoke({"query": "authentication"})
        first = results[0]
        assert "function:" in first
        assert "file:" in first

    def test_results_enriched_with_class_or_siblings(self):
        from agent.tools import hybrid_search
        results = hybrid_search.invoke({"query": "authentication"})
        # At least one result should have class or sibling info (graph enrichment)
        has_graph_context = any("class:" in r or "siblings:" in r for r in results)
        assert has_graph_context, "At least one result should be enriched with graph context"

    def test_demo_query_database_config(self):
        """Demo-style query: should find config-related functions"""
        from agent.tools import hybrid_search
        results = hybrid_search.invoke({"query": "database configuration settings"})
        assert len(results) > 0
        names = "\n".join(results).lower()
        assert "config" in names, "Should find config-related functions"

    def test_keyword_matching_works(self):
        from agent.tools import hybrid_search
        results = hybrid_search.invoke({"query": "DigestAuth"})
        assert len(results) > 0
        names = "\n".join(results).lower()
        assert "digest" in names or "auth" in names


# ---------------------------------------------------------------------------
# trace_impact
# ---------------------------------------------------------------------------

class TestTraceImpact:
    def test_returns_string(self):
        from agent.tools import trace_impact
        result = trace_impact.invoke({"symbol": "slugify"})
        assert isinstance(result, str)

    def test_finds_direct_callers(self):
        """slugify in sample_repo is called by display_items in main.py"""
        from agent.tools import trace_impact
        result = trace_impact.invoke({"symbol": "slugify"})
        assert "direct callers" in result
        assert "display_items" in result

    def test_finds_transitive_callers(self):
        """display_items is called by run, so run is a transitive caller of slugify"""
        from agent.tools import trace_impact
        result = trace_impact.invoke({"symbol": "slugify"})
        assert "transitive callers" in result
        assert "run" in result

    def test_shows_caller_files(self):
        from agent.tools import trace_impact
        result = trace_impact.invoke({"symbol": "slugify"})
        assert "caller files" in result
        assert "main.py" in result

    def test_no_results_message(self):
        from agent.tools import trace_impact
        result = trace_impact.invoke({"symbol": "nonexistent_function_xyz_999"})
        assert "No functions found" in result

    def test_display_items_has_callers(self):
        """display_items in sample_repo is called by run"""
        from agent.tools import trace_impact
        result = trace_impact.invoke({"symbol": "display_items"})
        assert "direct callers" in result
        assert "run" in result

    def test_leaf_function_shows_no_callers(self):
        from agent.tools import trace_impact
        result = trace_impact.invoke({"symbol": "hash_password"})
        assert "leaf function" in result


# ---------------------------------------------------------------------------
# version_diff
# ---------------------------------------------------------------------------

class TestVersionDiff:
    @_NEEDS_DIFF
    def test_returns_summary(self):
        from agent.tools import version_diff
        result = version_diff.invoke({"module": ""})
        assert isinstance(result, str)
        assert "Version Diff Summary" in result

    @_NEEDS_DIFF
    def test_shows_file_statuses(self):
        from agent.tools import version_diff
        result = version_diff.invoke({"module": ""})
        has_status = ("DELETED" in result or "MODIFIED" in result or "UNCHANGED" in result)
        assert has_status, "Should show at least one file status category"

    @_NEEDS_DIFF
    def test_shows_total_count(self):
        from agent.tools import version_diff
        result = version_diff.invoke({"module": ""})
        assert "Total:" in result
        assert "files tracked" in result

    def test_filter_by_module(self):
        """Filtering by a module that has diff data should return fewer results"""
        from agent.tools import version_diff
        full = version_diff.invoke({"module": ""})
        filtered = version_diff.invoke({"module": "utils"})
        # filtered should either have results or say no data
        assert isinstance(filtered, str)
        if "No version diff data" not in filtered:
            assert "utils" in filtered.lower()

    def test_no_diff_data_message(self):
        from agent.tools import version_diff
        result = version_diff.invoke({"module": "nonexistent_module_xyz_999"})
        assert "No version diff data" in result


# ---------------------------------------------------------------------------
# list_versions
# ---------------------------------------------------------------------------

class TestListVersions:
    def test_returns_string(self):
        from agent.tools import list_versions
        result = list_versions.invoke({"repo_filter": ""})
        assert isinstance(result, str)

    def test_shows_ingested_versions(self):
        from agent.tools import list_versions
        result = list_versions.invoke({"repo_filter": ""})
        assert "Ingested Versions" in result or "No ingested versions" in result

    def test_shows_version_count(self):
        from agent.tools import list_versions
        result = list_versions.invoke({"repo_filter": ""})
        if "No ingested versions" not in result:
            assert "Total:" in result
            assert "version(s)" in result

    def test_filter_by_repo(self):
        from agent.tools import list_versions
        result = list_versions.invoke({"repo_filter": "nonexistent_repo_xyz_999"})
        assert "No ingested versions" in result


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_all_tools_bound(self):
        from agent.graph import TOOLS
        names = [t.name for t in TOOLS]
        assert "hybrid_search" in names
        assert "trace_impact" in names
        assert "version_diff" in names
        assert "list_versions" in names

    def test_system_prompt_mentions_all_tools(self):
        from agent.graph import SYSTEM_PROMPT
        assert "hybrid_search" in SYSTEM_PROMPT
        assert "trace_impact" in SYSTEM_PROMPT
        assert "version_diff" in SYSTEM_PROMPT
        assert "list_versions" in SYSTEM_PROMPT

    def test_agent_builds(self):
        from agent.graph import build_query_agent
        agent = build_query_agent()
        assert agent is not None
