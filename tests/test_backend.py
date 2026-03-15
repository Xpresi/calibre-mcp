"""Tests for CalibreBackend — works with a real metadata.db if available."""

import os
import pytest
from calibre_mcp.config import Config
from calibre_mcp.backend import CalibreBackend


@pytest.fixture
def config():
    """Load config, skip tests if library is not accessible."""
    cfg = Config.load()
    errors = cfg.validate()
    if any("metadata.db" in e for e in errors) or any("Library path" in e for e in errors):
        pytest.skip("Calibre library not accessible — skipping integration tests")
    return cfg


@pytest.fixture
def backend(config):
    return CalibreBackend(config)


class TestLibraryStats:
    def test_returns_expected_keys(self, backend):
        stats = backend.get_library_stats()
        assert "total_books" in stats
        assert "total_authors" in stats
        assert "total_series" in stats
        assert "total_tags" in stats
        assert "total_publishers" in stats
        assert "formats" in stats
        assert "database_size_mb" in stats
        assert stats["total_books"] > 0

    def test_format_counts_are_positive(self, backend):
        stats = backend.get_library_stats()
        for fmt, count in stats["formats"].items():
            assert count > 0, f"Format {fmt} has 0 books"


class TestQueryDb:
    def test_simple_query(self, backend):
        rows = backend.query_db("SELECT COUNT(*) as cnt FROM books")
        assert len(rows) == 1
        assert rows[0]["cnt"] > 0

    def test_query_with_params(self, backend):
        rows = backend.query_db(
            "SELECT id, title FROM books WHERE title LIKE ? LIMIT 5",
            ("%King%",)
        )
        assert isinstance(rows, list)
        for row in rows:
            assert "id" in row
            assert "title" in row

    def test_scalar_query(self, backend):
        count = backend.query_db_scalar("SELECT COUNT(*) FROM books")
        assert isinstance(count, int)
        assert count > 0
